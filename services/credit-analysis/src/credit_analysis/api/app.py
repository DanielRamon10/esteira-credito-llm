"""Application factory da API.

`criar_app()` recebe os adapters por parametro. Isso e o que permite ao teste
de integracao subir a aplicacao inteira com um bureau deterministico e um
repositorio limpo, sem monkeypatch e sem variavel global.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, Response
from plataforma import autenticacao as autenticacao_compartilhada
from plataforma import llm as llm_compartilhado
from plataforma import seguranca
from plataforma.llm import (
    LLMOllama,
    criar_chat_ollama,
    modelos_instalados,
    ollama_disponivel,
)
from plataforma.logging import configurar_logging
from plataforma.metricas import rotulo_de_rota

from credit_analysis.api.errors import registrar_handlers
from credit_analysis.api.routers import agente as rota_agente
from credit_analysis.api.routers import analises, documentos, health, politicas
from credit_analysis.api.routers import metricas as rota_metricas
from credit_analysis.api.seguranca import montar_chaveiro
from credit_analysis.application.ports import (
    AgenteCredito,
    ArmazenamentoDocumentos,
    ConsultaBureau,
    ConsultaKYC,
    FilaDeTrabalho,
    ModeloLinguagem,
    MotorOCR,
    RepositorioAnalises,
)
from credit_analysis.config import ProvedorLLM, Settings, get_settings
from credit_analysis.infrastructure.agente.grafo import AgenteLangGraph
from credit_analysis.infrastructure.armazenamento.memoria import (
    ArmazenamentoEmMemoria,
    FilaEmMemoria,
)
from credit_analysis.infrastructure.armazenamento.s3 import ArmazenamentoS3
from credit_analysis.infrastructure.armazenamento.sqs import FilaSQS
from credit_analysis.infrastructure.bureau import BureauStub
from credit_analysis.infrastructure.kyc import ClienteKYCHttp
from credit_analysis.infrastructure.llm.anthropic_adapter import LLMAnthropic, LLMFake
from credit_analysis.infrastructure.observabilidade import metricas
from credit_analysis.infrastructure.observabilidade.tracing import (
    configurar_tracing,
    instrumentar_fastapi,
)
from credit_analysis.infrastructure.ocr.escalonamento import MotorOCRComEscalonamento
from credit_analysis.infrastructure.ocr.tesseract import OCRTesseract, localizar_binario
from credit_analysis.infrastructure.ocr.vision import OCRClaudeVision
from credit_analysis.infrastructure.rag.embeddings import EmbedderFastEmbed
from credit_analysis.infrastructure.rag.pgvector_store import VectorStorePgVector, criar_pool
from credit_analysis.infrastructure.rag.retriever import RetrieverHibrido
from credit_analysis.infrastructure.repositories.memoria import RepositorioAnalisesMemoria
from credit_analysis.infrastructure.tokens import (
    ProvedorDeToken,
    TokenDeClientCredentials,
    TokenEstatico,
)

logger = structlog.get_logger(__name__)

_observadores_ligados = False

CABECALHO_CORRELACAO = "X-Request-ID"


def criar_app(
    settings: Settings | None = None,
    repositorio: RepositorioAnalises | None = None,
    bureau: ConsultaBureau | None = None,
    retriever: RetrieverHibrido | None = None,
    llm: ModeloLinguagem | None = None,
    motor_ocr: MotorOCR | None = None,
    agente: AgenteCredito | None = None,
    kyc: ConsultaKYC | None = None,
    armazenamento: ArmazenamentoDocumentos | None = None,
    fila: FilaDeTrabalho | None = None,
) -> FastAPI:
    """Monta a aplicacao. Adapters omitidos caem no default de desenvolvimento."""
    settings = settings or get_settings()
    configurar_logging(nivel=settings.nivel_log, formato_json=settings.log_json)
    configurar_tracing(
        endpoint=settings.otlp_endpoint,
        nome_servico=settings.nome_servico,
        versao=settings.versao,
        ambiente=settings.ambiente.value,
        amostragem=settings.trace_amostragem,
    )
    metricas.registrar_info(
        versao=settings.versao,
        ambiente=settings.ambiente.value,
        provedor_llm=settings.provedor_llm.value,
        modelo_agente=settings.modelo_agente,
    )
    _ligar_observadores_da_plataforma()

    # O pool so existe quando o RAG e montado a partir da configuracao; quando
    # o retriever vem injetado (teste), quem injetou cuida do ciclo de vida.
    pool = (
        criar_pool(settings.postgres_dsn) if retriever is None and settings.usar_pgvector else None
    )

    @asynccontextmanager
    async def lifespan(app_: FastAPI) -> AsyncGenerator[None]:
        logger.info(
            "servico.iniciando",
            servico=settings.nome_servico,
            versao=settings.versao,
            ambiente=settings.ambiente.value,
            rag=("pgvector" if pool else "injetado" if retriever else "desabilitado"),
            llm=app_.state.llm.identificacao,
            kyc=(app_.state.kyc.identificacao if app_.state.kyc else "desabilitado"),
            ocr=(app_.state.motor_ocr.identificacao if app_.state.motor_ocr else "indisponivel"),
        )

        if pool is not None:
            # Aberto no lifespan e nao no construtor: e aqui que existe event
            # loop, e e aqui que o fechamento tambem esta garantido.
            await pool.open(wait=True, timeout=30)
            # O embedder e construido agora mas o modelo so carrega na primeira
            # consulta (cached_property) — 2,24GB nao devem atrasar o boot nem
            # ser pagos por uma replica que nunca recebe consulta de politica.
            app_.state.retriever = RetrieverHibrido(
                VectorStorePgVector(pool), EmbedderFastEmbed(settings.modelo_embedding)
            )

        # O agente e montado aqui, e nao junto dos outros adapters, porque ele
        # depende do retriever — que com pgvector so existe depois do bloco
        # acima. Montar antes deixaria o agente permanentemente sem a ferramenta
        # de politica, e o sintoma seria "o agente nunca consulta politica",
        # facil de confundir com o modelo escolhendo mal a ferramenta.
        if app_.state.agente is None:
            app_.state.agente = _montar_agente(
                settings,
                retriever=app_.state.retriever,
                repositorio=app_.state.repositorio,
            )
        logger.info(
            "servico.agente",
            agente=(app_.state.agente.identificacao if app_.state.agente else "indisponivel"),
        )

        tarefa_trabalhador: asyncio.Task[None] | None = None
        if settings.trabalhador_em_processo:
            tarefa_trabalhador = asyncio.create_task(
                _laco_do_trabalhador(app_), name="trabalhador-extracao"
            )
            logger.info("trabalhador.em_processo_iniciado")

        yield

        if tarefa_trabalhador is not None:
            # Cancela e **espera** o cancelamento.
            #
            # Sem o `await`, o `lifespan` retorna com a tarefa ainda em voo: o event loop fecha
            # embaixo dela e o asyncio reclama de "task was destroyed but it is pending". Pior,
            # uma extracao no meio da aplicacao perderia o `salvar` — o documento ficaria
            # `extraindo` e a mensagem voltaria para a fila, o que se recupera, mas com uma
            # extracao inteira jogada fora a cada deploy.
            tarefa_trabalhador.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tarefa_trabalhador
            logger.info("trabalhador.em_processo_encerrado")

        # Fecha o pool de conexoes HTTP do cliente de KYC junto com o resto: sem
        # isso o httpx reclama de cliente nao fechado no encerramento, e conexoes
        # keep-alive ficam penduradas no outro servico.
        cliente_kyc = getattr(app_.state, "kyc", None)
        if cliente_kyc is not None and hasattr(cliente_kyc, "fechar"):
            await cliente_kyc.fechar()

        if pool is not None:
            await pool.close()
        logger.info("servico.encerrando", servico=settings.nome_servico)

    app = FastAPI(
        title="Credit Analysis API",
        description=(
            "Esteira de analise de credito com extracao documental, "
            "consulta a politicas internas e parecer explicavel."
        ),
        version=settings.versao,
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_habilitados else None,
        redoc_url="/redoc" if settings.docs_habilitados else None,
        openapi_url="/openapi.json" if settings.docs_habilitados else None,
    )

    # Chaveiro montado **no boot**, nao por requisicao.
    #
    # Duas razoes. A primeira e custo: com JWKS, construir por requisicao refaria a chamada
    # HTTP ao IdP e jogaria fora o cache. A segunda e mais importante — `montar_chaveiro`
    # levanta se a configuracao estiver incompleta, e o lugar certo para isso falhar e a
    # subida do processo. Falhando por requisicao, o servico ficaria de pe respondendo 500 a
    # tudo, e um `/health` que nao toca no chaveiro diria "ok".
    app.state.chaveiro = montar_chaveiro(settings)

    app.state.settings = settings
    app.state.repositorio = repositorio or RepositorioAnalisesMemoria()
    app.state.bureau = bureau or BureauStub()
    app.state.retriever = retriever  # pode virar pgvector no lifespan
    app.state.motor_ocr = motor_ocr or _montar_ocr(settings)
    app.state.llm = llm or _montar_llm(settings)
    app.state.agente = agente  # montado no lifespan quando nao injetado
    app.state.armazenamento = armazenamento or _montar_armazenamento(settings)
    app.state.fila = fila or _montar_fila(settings)
    app.state.kyc = kyc or _montar_kyc(settings)

    @app.middleware("http")
    async def correlacao_log_e_metricas(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Propaga um request id, loga e mede cada requisicao.

        Sem correlation id, rastrear uma requisicao que passou por tres
        servicos vira arqueologia de timestamp. Com ele, um filtro resolve.
        """
        request_id = request.headers.get(CABECALHO_CORRELACAO) or str(uuid.uuid4())

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            metodo=request.method,
            rota=request.url.path,
        )

        inicio = time.perf_counter()
        # Rotulo generico e nao a rota: aqui, ANTES do roteamento, o template
        # ainda nao existe — usar o caminho cru traria de volta o problema de
        # cardinalidade que `_rotulo_de_rota` resolve. Este gauge responde
        # "quantas requisicoes estao abertas agora", o sinal de saturacao;
        # latencia por rota sai do histograma.
        metricas.http_em_andamento.labels(rota="total").inc()
        try:
            response: Response = await call_next(request)
        finally:
            metricas.http_em_andamento.labels(rota="total").dec()

        duracao = time.perf_counter() - inicio
        rota = _rotulo_de_rota(request)

        metricas.http_duracao.labels(metodo=request.method, rota=rota).observe(duracao)
        metricas.http_requisicoes.labels(
            metodo=request.method, rota=rota, status=str(response.status_code)
        ).inc()

        response.headers[CABECALHO_CORRELACAO] = request_id
        logger.info("http.requisicao", status=response.status_code, duracao_ms=int(duracao * 1000))
        return response

    registrar_handlers(app)

    app.include_router(rota_metricas.router)
    app.include_router(health.router)
    app.include_router(analises.router, prefix=settings.prefixo_api)
    app.include_router(politicas.router, prefix=settings.prefixo_api)
    app.include_router(documentos.router, prefix=settings.prefixo_api)
    # Router separado: a consulta vive em `/documentos/{id}`, fora do prefixo `/analises`.
    app.include_router(documentos.consulta, prefix=settings.prefixo_api)
    app.include_router(rota_agente.router, prefix=settings.prefixo_api)

    # Depois dos routers: o instrumentador percorre as rotas registradas para
    # nomear os spans com o template, e nao com o caminho cru.
    instrumentar_fastapi(app)

    return app


def _ligar_observadores_da_plataforma() -> None:
    """Liga os ganchos da `plataforma` as metricas deste servico.

    A biblioteca compartilhada nao conhece Prometheus — se conhecesse, todo
    consumidor futuro ficaria preso a esta stack de observabilidade. Ela apenas
    avisa que algo aconteceu, e a traducao para contador vive aqui, no composition
    root, junto das outras decisoes de wiring.
    """
    # Idempotente: `criar_app` e chamada dezenas de vezes na suite, e sem esta
    # guarda cada chamada empilharia mais um observador — o contador passaria a
    # incrementar N vezes por evento, e o painel mentiria para cima.
    global _observadores_ligados
    if _observadores_ligados:
        return

    seguranca.registrar_observador(
        lambda superficie, categoria: metricas.injecao_detectada.labels(
            superficie=superficie, categoria=categoria
        ).inc()
    )
    llm_compartilhado.registrar_observador(_medir_llm)
    autenticacao_compartilhada.registrar_observador(_medir_autenticacao)
    _observadores_ligados = True


def _medir_autenticacao(evento: str, motivo: str) -> None:
    """Traduz o gancho de autenticacao da plataforma na metrica deste servico.

    `aceito` tambem e contado, e nao apenas as negativas. Sem o denominador, "50 negativas
    em 10 minutos" nao distingue um cliente recem-integrado com configuracao errada de uma
    tentativa de forca bruta — o que separa os dois e a **proporcao** sobre o total, e ela
    nao existe sem contar o sucesso.
    """
    metricas.auth_decisoes.labels(evento=evento, motivo=motivo).inc()


def _medir_llm(
    modelo: str,
    resultado: str,
    duracao: float,
    entrada: int | None,
    saida: int | None,
) -> None:
    metricas.llm_chamadas.labels(modelo=modelo, operacao="gerar", resultado=resultado).inc()
    if resultado == "ok":
        metricas.llm_duracao.labels(modelo=modelo, operacao="gerar").observe(duracao)
    for direcao, quantidade in (("entrada", entrada), ("saida", saida)):
        if quantidade is not None:
            metricas.llm_tokens.labels(modelo=modelo, direcao=direcao).inc(quantidade)


async def _laco_do_trabalhador(app_: FastAPI) -> None:
    """Laco do trabalhador em processo.

    Le os adapters de `app_.state` e nao os monta de novo: o armazenamento e a fila precisam ser
    **os mesmos** que a API usa. Montando outros, o trabalhador em memoria consumiria uma fila
    vazia — e com S3/SQS funcionaria por acidente, ate alguem trocar um endpoint num lugar so.

    Excecao aqui **nao** derruba a API: um trabalhador que morre deixa a fila crescendo, o que o
    alerta pega, enquanto uma API derrubada por falha de extracao para de aceitar analise. A
    ordem de gravidade e clara.
    """
    from credit_analysis.application.use_cases.extracao_assincrona import ExtrairDocumento
    from credit_analysis.application.use_cases.processar_documento import AplicarExtracao
    from credit_analysis.application.use_cases.trabalhador import Trabalhador, laco

    trabalhador = Trabalhador(
        fila=app_.state.fila,
        extrair=ExtrairDocumento(
            armazenamento=app_.state.armazenamento, motor_ocr=app_.state.motor_ocr
        ),
        aplicar=AplicarExtracao(repositorio=app_.state.repositorio, bureau=app_.state.bureau),
        repositorio=app_.state.repositorio,
    )

    try:
        await laco(trabalhador)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.error("trabalhador.em_processo_morreu", exc_info=True)


def _montar_armazenamento(settings: Settings) -> ArmazenamentoDocumentos:
    """S3 quando configurado, memoria fora de producao, erro em producao.

    A mesma assimetria de `_montar_kyc`, e pela mesma razao: documento de cliente num dicionario
    de processo desaparece no primeiro restart. A POL-006 secao 5 exige guardar o original por 5
    anos, e um servico que cumpre isso em memoria nao cumpre — ele apenas nao reclama.

    Fora de producao o adapter em memoria e legitimo: e o que permite rodar a suite e o compose
    sem MinIO no ar.
    """
    if not settings.usar_armazenamento_real:
        if settings.producao:
            raise RuntimeError(
                "CREDIT_BUCKET_DOCUMENTOS e CREDIT_FILA_EXTRACAO_URL sao obrigatorias em "
                "producao: em memoria, o documento que embasou um parecer desaparece no "
                "primeiro restart, descumprindo a POL-006 secao 5."
            )
        logger.warning(
            "armazenamento.em_memoria",
            motivo="CREDIT_BUCKET_DOCUMENTOS ou CREDIT_FILA_EXTRACAO_URL vazia",
            efeito="documentos nao sobrevivem ao restart",
        )
        return ArmazenamentoEmMemoria()

    return ArmazenamentoS3(
        bucket=settings.bucket_documentos,
        regiao=settings.regiao_aws,
        endpoint_url=settings.s3_endpoint or None,
    )


def _montar_fila(settings: Settings) -> FilaDeTrabalho:
    """SQS quando configurada, memoria fora de producao.

    A fila em memoria tem uma propriedade que a de verdade nao tem: ela morre com o processo. Isso
    e aceitavel em desenvolvimento e seria perda de trabalho em producao — um deploy no meio de
    uma extracao apagaria o pedido, e o documento ficaria `recebido` sem nada para retoma-lo.
    """
    if not settings.usar_armazenamento_real:
        return FilaEmMemoria()

    return FilaSQS(
        url_da_fila=settings.fila_extracao_url,
        regiao=settings.regiao_aws,
        endpoint_url=settings.sqs_endpoint or None,
    )


def _montar_kyc(settings: Settings) -> ConsultaKYC | None:
    """Monta o cliente de conformidade, ou recusa a subir.

    A assimetria entre ambientes e deliberada e e a decisao central desta funcao:

    - **Fora de producao**, sem URL configurada o gate simplesmente nao existe. E o
      que permite desenvolver e rodar a suite sem subir um segundo servico.
    - **Em producao**, a ausencia de URL levanta erro na subida. Uma esteira que
      aprova credito sem triagem de lista restritiva descumpre a Circular BCB 3.978,
      e fazer isso por configuracao faltando — em silencio — e pior que ficar fora
      do ar: ninguem percebe ate a auditoria.

    E a mesma disciplina do `CREDIT_PROVEDOR_LLM=ollama`: pedir explicitamente algo
    indisponivel falha alto, em vez de degradar para um substituto que se parece com
    o original.
    """
    if not settings.kyc_url.strip():
        if settings.producao:
            raise RuntimeError(
                "CREDIT_KYC_URL e obrigatoria em producao: sem o servico de "
                "conformidade a esteira aprovaria credito sem triagem de lista "
                "restritiva, descumprindo a Circular BCB 3.978."
            )
        logger.warning(
            "kyc.desabilitado",
            motivo="CREDIT_KYC_URL vazia",
            efeito="o gate de conformidade nao sera aplicado",
        )
        return None

    return ClienteKYCHttp(
        url_base=settings.kyc_url,
        timeout_segundos=settings.kyc_timeout_segundos,
        tentativas=settings.kyc_tentativas,
        provedor_de_token=_montar_provedor_de_token(settings),
    )


def _montar_provedor_de_token(settings: Settings) -> ProvedorDeToken | None:
    """Credencial de servico para chamar o KYC, ou recusa a subir em producao.

    A mesma assimetria de `_montar_kyc`, e pelo mesmo motivo: fora de producao, sem credencial
    o cliente chama o KYC sem token — o que hoje resulta em 401 e vira revisao humana, o que e
    aceitavel em desenvolvimento. Em producao, subir sem credencial garantiria que **toda**
    analise cai em revisao manual, e o `/health` diria "ok" enquanto a fila cresce.

    A ordem confere `kyc_token_url` primeiro: se as duas formas estiverem configuradas, a do
    IdP ganha. E ha um erro explicito quando as duas aparecem, porque a alternativa —
    escolher em silencio — deixaria um token estatico esquecido na configuracao parecendo
    inofensivo enquanto na verdade nao e usado, e alguem o renovaria sem efeito.
    """
    tem_idp = bool(settings.kyc_token_url.strip())
    tem_estatico = bool(settings.kyc_token.strip())

    if tem_idp and tem_estatico:
        raise RuntimeError(
            "CREDIT_KYC_TOKEN_URL e CREDIT_KYC_TOKEN sao mutuamente exclusivas: com as duas, "
            "o token estatico ficaria na configuracao sem ser usado."
        )

    if tem_idp:
        if not (settings.kyc_client_id.strip() and settings.kyc_client_secret.strip()):
            raise RuntimeError(
                "CREDIT_KYC_TOKEN_URL exige CREDIT_KYC_CLIENT_ID e CREDIT_KYC_CLIENT_SECRET."
            )
        return TokenDeClientCredentials(
            url_do_token=settings.kyc_token_url,
            client_id=settings.kyc_client_id,
            client_secret=settings.kyc_client_secret,
            audiencia="kyc-compliance",
        )

    if tem_estatico:
        return TokenEstatico(settings.kyc_token)

    if settings.producao:
        raise RuntimeError(
            "o KYC exige credencial: configure CREDIT_KYC_TOKEN_URL (client_credentials) ou "
            "CREDIT_KYC_TOKEN. Sem ela, toda analise cai em revisao manual em silencio."
        )

    logger.warning(
        "kyc.sem_credencial",
        motivo="CREDIT_KYC_TOKEN e CREDIT_KYC_TOKEN_URL vazias",
        efeito="as chamadas ao KYC sairao sem token e serao recusadas com 401",
    )
    return None


def _rotulo_de_rota(request: Request) -> str:
    """Adapta o `Request` do Starlette ao helper da plataforma.

    A logica morava aqui inteira, escrita antes de existir uma biblioteca compartilhada.
    Quando o terceiro servico apareceu, mantê-la neste arquivo teria produzido tres
    copias de uma funcao que **ja esteve errada uma vez** — a primeira versao usava
    `route.path`, que omite o prefixo do `include_router` e somaria `/v1` e um futuro
    `/v2` na mesma serie temporal. Triplicar codigo com esse historico e exatamente o que
    a extracao serviu para evitar.

    O que sobra aqui e so a traducao: `plataforma` nao depende de FastAPI, entao o
    servico extrai do `scope` o que o helper precisa e passa valores simples. Ver
    `plataforma.metricas.rotulo_de_rota` para o raciocinio de cardinalidade e para a
    defesa contra varredura de URL.
    """
    return rotulo_de_rota(
        request.url.path,
        request.scope.get("path_params"),
        casou_com_rota=request.scope.get("route") is not None,
    )


def _montar_llm(settings: Settings) -> ModeloLinguagem:
    """Escolhe o adapter de LLM conforme a configuracao e o ambiente.

    Em `auto` a ordem e: Anthropic (se houver chave) -> Ollama (se o daemon
    responder) -> fake. Nenhuma dessas etapas exige configuracao, entao o
    servico sobe em qualquer maquina; o que muda e a qualidade da redacao, e
    o adapter em uso aparece no log de inicializacao e no campo `motor` do
    parecer.
    """
    escolhido = settings.provedor_llm

    if escolhido is ProvedorLLM.FAKE:
        return LLMFake()

    if escolhido in {ProvedorLLM.ANTHROPIC, ProvedorLLM.AUTO} and settings.usar_llm_real:
        return LLMAnthropic(
            modelo=settings.modelo_llm, timeout_segundos=settings.llm_timeout_segundos
        )

    if escolhido is ProvedorLLM.ANTHROPIC:
        # Pedido explicitamente e sem chave: falhar aqui e melhor que cair no
        # fake em silencio e alguem descobrir na revisao do parecer.
        raise RuntimeError(
            "CREDIT_PROVEDOR_LLM=anthropic exige CREDIT_ANTHROPIC_API_KEY configurada"
        )

    if escolhido in {ProvedorLLM.OLLAMA, ProvedorLLM.AUTO}:
        if ollama_disponivel(settings.ollama_endpoint):
            instalados = modelos_instalados(settings.ollama_endpoint)
            if instalados and settings.modelo_ollama not in instalados:
                logger.warning(
                    "llm.modelo_ollama_ausente",
                    solicitado=settings.modelo_ollama,
                    instalados=list(instalados),
                    acao=f"rode: ollama pull {settings.modelo_ollama}",
                )
            return LLMOllama(
                modelo=settings.modelo_ollama,
                endpoint=settings.ollama_endpoint,
                timeout_segundos=settings.ollama_timeout_segundos,
            )

        if escolhido is ProvedorLLM.OLLAMA:
            raise RuntimeError(
                f"CREDIT_PROVEDOR_LLM=ollama mas o daemon nao responde em "
                f"{settings.ollama_endpoint}. Instale com "
                f"`winget install Ollama.Ollama` e rode `ollama serve`."
            )

    logger.warning("llm.usando_fake", motivo="nenhum provedor real disponivel")
    return LLMFake()


def _montar_agente(
    settings: Settings,
    retriever: RetrieverHibrido | None,
    repositorio: RepositorioAnalises,
) -> AgenteCredito | None:
    """Monta o agente, ou devolve None quando nao ha modelo com ferramentas.

    Diferente do `_montar_llm`, aqui **nao existe fake de producao**. Um agente
    que finge decidir e pior que um agente ausente: a resposta tem a mesma
    aparencia da real, com trilha e tudo, e nada no corpo do JSON avisa que as
    ferramentas nunca rodaram. Sem modelo, o endpoint responde 503 dizendo o que
    instalar.

    Note que o agente sobe **mesmo sem indice de politicas**. Ele perde a
    ferramenta de consulta e mantem a simulacao, que e deterministica e nao
    depende de nada externo — degradacao parcial e melhor que 503 total, desde
    que o modelo saiba quais ferramentas tem (e ele sabe: a caixa so anuncia as
    que funcionam).
    """
    if settings.provedor_llm is ProvedorLLM.FAKE:
        logger.info("agente.desabilitado", motivo="provedor_llm=fake")
        return None

    modelo: object | None = None
    identificacao = ""

    if (
        settings.provedor_llm in {ProvedorLLM.ANTHROPIC, ProvedorLLM.AUTO}
        and settings.usar_llm_real
    ):
        from langchain_anthropic import ChatAnthropic

        modelo = ChatAnthropic(
            model=settings.modelo_llm,
            timeout=settings.llm_timeout_segundos,
            max_tokens_to_sample=2048,
            stop=None,
        )
        identificacao = f"anthropic:{settings.modelo_llm}"

    elif settings.provedor_llm in {ProvedorLLM.OLLAMA, ProvedorLLM.AUTO} and ollama_disponivel(
        settings.ollama_endpoint
    ):
        instalados = modelos_instalados(settings.ollama_endpoint)
        if instalados and settings.modelo_agente not in instalados:
            # Aviso e nao erro: o Ollama baixa sob demanda na primeira chamada.
            # O que nao pode acontecer e a primeira requisicao de negocio pagar
            # um download de 4GB sem ninguem entender a demora.
            logger.warning(
                "agente.modelo_ausente",
                solicitado=settings.modelo_agente,
                instalados=list(instalados),
                acao=f"rode: ollama pull {settings.modelo_agente}",
            )
        modelo = criar_chat_ollama(
            modelo=settings.modelo_agente,
            endpoint=settings.ollama_endpoint,
            timeout_segundos=settings.ollama_timeout_segundos,
        )
        identificacao = f"ollama:{settings.modelo_agente}"

    if modelo is None:
        logger.warning("agente.indisponivel", motivo="nenhum modelo com suporte a ferramentas")
        return None

    from langchain_core.language_models import BaseChatModel

    assert isinstance(modelo, BaseChatModel)
    return AgenteLangGraph(
        modelo=modelo,
        retriever=retriever,
        repositorio=repositorio,
        identificacao=identificacao,
        max_passos=settings.agente_max_passos,
        orcamento_segundos=settings.agente_timeout_segundos,
    )


def _montar_ocr(settings: Settings) -> MotorOCR | None:
    """Monta a cadeia de OCR conforme o que esta disponivel no ambiente.

    A ordem e por custo: Tesseract local primeiro, modelo de visao depois. Se
    nenhum dos dois estiver disponivel, devolve None e o endpoint responde 503
    com instrucao — em vez de a aplicacao nao subir por causa de uma capacidade
    opcional.
    """
    motores: list[MotorOCR] = []

    if localizar_binario() is not None:
        motores.append(OCRTesseract())

    if settings.usar_llm_real:
        motores.append(
            OCRClaudeVision(
                modelo=settings.modelo_llm,
                api_key=settings.anthropic_api_key,
            )
        )

    if not motores:
        logger.warning("ocr.nenhum_motor_disponivel")
        return None

    if len(motores) == 1:
        return motores[0]

    # O verificador de suficiencia decide o escalonamento por campos extraidos,
    # nao por media de confianca — ver o cabecalho de `escalonamento.py`.
    from credit_analysis.infrastructure.ocr.extracao import holerite_suficiente

    return MotorOCRComEscalonamento(motores, suficiencia=holerite_suficiente)


# **Nao ha `app = criar_app()` em nivel de modulo, e a ausencia e deliberada.**
#
# Havia, e ela construia a aplicacao inteira a cada **import** do modulo. O sintoma apareceu
# na Camada 7: como autenticacao nao tem modo desligado, `criar_app()` no import passou a
# levantar quando a chave nao esta configurada — e a suite inteira falhava na coleta, com uma
# mensagem sobre autenticacao vinda de um arquivo que trata de pgvector.
#
# O erro era o sintoma, nao a causa. Importar um modulo nao deveria abrir pool de conexao,
# ler configuracao do ambiente nem carregar corpus; ferramenta de analise estatica,
# autocompletar de IDE e coleta de teste importam modulos o tempo todo.
#
# O uvicorn recebe uma **factory** (`factory=True` no `__main__.py`), que e o mecanismo
# proprio dele para isto.
