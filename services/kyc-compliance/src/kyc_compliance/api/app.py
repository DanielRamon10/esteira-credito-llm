"""Application factory do kyc-compliance.

Mesma forma do outro servico — `criar_app()` recebe adapters por parametro — mas
com uma diferenca que vale notar: aqui **nao ha degradacao para fake**.

No `credit-analysis` um LLM indisponivel cai num fake deterministico, porque a
esteira ainda produz parecer sem redacao. Aqui a lista *e* o servico: sem ela nao
existe triagem possivel, e responder "nenhuma correspondencia" com a lista vazia
seria aprovar todo mundo. Entao a construcao falha, o pod nao passa no readiness e
o rollout para.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from plataforma import autenticacao as autenticacao_compartilhada
from plataforma.logging import configurar_logging
from plataforma.metricas import rotulo_de_rota

from kyc_compliance.api.routers import health, triagens
from kyc_compliance.api.routers import metricas as rota_metricas
from kyc_compliance.api.schemas import ErroResponse
from kyc_compliance.api.seguranca import montar_chaveiro
from kyc_compliance.application.ports import RepositorioListas, RepositorioTriagens
from kyc_compliance.config import Settings, get_settings
from kyc_compliance.infrastructure import metricas
from kyc_compliance.infrastructure.listas import ListasDeArquivo
from kyc_compliance.infrastructure.repositories.memoria import RepositorioTriagensMemoria

logger = structlog.get_logger(__name__)

CABECALHO_CORRELACAO = "X-Request-ID"


# Este servico NAO liga observador de injecao, ao contrario do `credit-analysis` e do
# `customer-support`. O motivo: ele nao processa conteudo nao confiavel — recebe nome e
# CPF validados na borda e compara contra lista propria. Registrar um gancho para um
# sinal que nunca ocorre criaria uma serie temporal permanentemente em zero, que e
# ruido no painel e sugere cobertura que nao existe.


def criar_app(
    settings: Settings | None = None,
    listas: RepositorioListas | None = None,
    repositorio: RepositorioTriagens | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configurar_logging(nivel=settings.nivel_log, formato_json=settings.log_json)
    # Diferente do gancho de injecao, que este servico deliberadamente NAO liga (ele
    # nao processa conteudo nao confiavel), o de autenticacao vale aqui: toda
    # requisicao passa por validacao de token.
    autenticacao_compartilhada.registrar_observador(_medir_autenticacao)
    metricas.http.publicar_info(versao=settings.versao, ambiente=settings.ambiente.value)

    # Carregado aqui e nao no lifespan: se a lista nao existe, o objetivo e falhar
    # na construcao, antes de o servidor abrir porta. Falhar no lifespan tambem
    # funcionaria, mas deixaria a janela em que o processo esta vivo e inutil.
    listas_resolvidas = listas or ListasDeArquivo(settings.diretorio_listas)

    @asynccontextmanager
    async def lifespan(app_: FastAPI) -> AsyncGenerator[None]:
        logger.info(
            "servico.iniciando",
            servico=settings.nome_servico,
            versao=settings.versao,
            ambiente=settings.ambiente.value,
            entradas=app_.state.listas.total,
            procedencia=app_.state.listas.procedencia,
        )
        yield
        logger.info("servico.encerrando", servico=settings.nome_servico)

    app = FastAPI(
        title="KYC Compliance API",
        description=(
            "Triagem de clientes contra listas restritivas (PEP, sancoes, midia "
            "negativa), com decisao deterministica e explicavel."
        ),
        version=settings.versao,
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_habilitados else None,
        redoc_url="/redoc" if settings.docs_habilitados else None,
        openapi_url="/openapi.json" if settings.docs_habilitados else None,
    )

    # Chaveiro montado **no boot**, nao por requisicao.

    #

    # Com JWKS, construir por requisicao refaria a chamada HTTP ao IdP e jogaria

    # fora o cache. E `montar_chaveiro` levanta se a configuracao estiver

    # incompleta: o lugar certo para isso falhar e a subida do processo, nao a

    # primeira requisicao — o servico ficaria de pe respondendo 500 a tudo, com o

    # `/health` dizendo "ok".

    app.state.chaveiro = montar_chaveiro(settings)

    app.state.settings = settings
    app.state.listas = listas_resolvidas
    app.state.repositorio = repositorio or RepositorioTriagensMemoria()

    @app.middleware("http")
    async def correlacao_e_log(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Propaga o request id que vem do servico chamador.

        O cabecalho ser **reaproveitado** e nao regerado e o que permite seguir uma
        analise de credito que consultou o KYC: os dois servicos logam o mesmo id.
        Gerar um novo aqui quebraria a correlacao exatamente onde ela e util.
        """
        request_id = request.headers.get(CABECALHO_CORRELACAO) or str(uuid.uuid4())

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            servico=settings.nome_servico,
            metodo=request.method,
            rota=request.url.path,
        )

        inicio = time.perf_counter()
        metricas.http.em_andamento.inc()
        try:
            response: Response = await call_next(request)
        finally:
            metricas.http.em_andamento.dec()
        duracao = time.perf_counter() - inicio

        # Template da rota, nunca o caminho concreto: `/v1/triagens/<uuid>` como label
        # criaria uma serie temporal por triagem. A funcao vem da plataforma porque a
        # logica e sutil — ela ja esteve errada uma vez, omitindo o prefixo de versao.
        rota = rotulo_de_rota(
            request.url.path,
            request.scope.get("path_params"),
            casou_com_rota=request.scope.get("route") is not None,
        )
        metricas.http.registrar(request.method, rota, response.status_code, duracao)

        response.headers[CABECALHO_CORRELACAO] = request_id
        logger.info("http.requisicao", status=response.status_code, duracao_ms=int(duracao * 1000))
        return response

    @app.exception_handler(autenticacao_compartilhada.EscopoInsuficiente)
    async def _escopo(request: Request, exc: Exception) -> JSONResponse:
        """403, nunca 401 — e `EscopoInsuficiente` NAO herda de `TokenInvalido` para isso.

            401  "suas credenciais nao servem, tente outras"
            403  "suas credenciais servem e nao bastam"

        Devolver 401 aqui manda um cliente correto reautenticar num laco que nunca resolve, e
        esconde de quem opera que o problema e de permissao e nao de credencial.

        A mensagem nao diz qual escopo falta: enumerar escopos para quem nao os tem e
        entregar o mapa de permissoes do servico.
        """
        identidade = getattr(request.state, "identidade", None)
        logger.warning(
            "auth.escopo_insuficiente",
            sujeito=getattr(identidade, "sujeito", None),
            escopo_faltante=str(exc),
            path=request.url.path,
        )
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content=ErroResponse(
                codigo="escopo_insuficiente",
                mensagem="Credencial valida, sem permissao para esta operacao.",
            ).model_dump(),
        )

    @app.exception_handler(autenticacao_compartilhada.ErroDeAutenticacao)
    async def _autenticacao(request: Request, exc: Exception) -> JSONResponse:
        """401 com `WWW-Authenticate`, como manda a RFC 6750 secao 3.

        Sem o cabecalho, biblioteca HTTP que renova token sozinha nao dispara a renovacao, e o
        sintoma e "o cliente parou de funcionar" sem erro que aponte para o servidor.

        O `motivo` interno (expirado, audiencia_incorreta, ...) fica no log e na metrica, nao
        na resposta: dizer ao cliente que a audiencia estava errada confirma que existe um
        servico com outra audiencia.
        """
        motivo = getattr(exc, "motivo", "desconhecido")
        logger.warning("auth.negado", motivo=motivo, path=request.url.path)
        ausente = motivo == "ausente"
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={
                "WWW-Authenticate": (
                    'Bearer realm="kyc-compliance"'
                    if ausente
                    # Token ausente nao e token invalido: sem `error`, o cliente sabe que
                    # precisa apresentar credencial em vez de trocar a que tem.
                    else 'Bearer realm="kyc-compliance", error="invalid_token"'
                )
            },
            content=ErroResponse(
                codigo="nao_autenticado", mensagem="Credencial ausente ou invalida."
            ).model_dump(),
        )

    @app.exception_handler(RequestValidationError)
    async def validacao(_: Request, exc: RequestValidationError) -> JSONResponse:
        """Formato de erro igual ao do outro servico.

        Consistencia de contrato entre servicos do mesmo monorepo nao e estetica:
        o cliente escreve um unico tratamento de erro para os dois.
        """
        return JSONResponse(
            status_code=422,
            content={
                "codigo": "payload_invalido",
                "mensagem": "Dados de entrada invalidos",
                "detalhes": [
                    {"campo": ".".join(str(p) for p in e["loc"][1:]), "erro": e["msg"]}
                    for e in exc.errors()
                ],
            },
        )

    app.include_router(rota_metricas.router)
    app.include_router(health.router)
    app.include_router(triagens.router, prefix=settings.prefixo_api)

    return app


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


def _medir_autenticacao(evento: str, motivo: str) -> None:
    """Traduz o gancho de autenticacao da plataforma na metrica deste servico."""
    metricas.auth_decisoes.labels(evento=evento, motivo=motivo).inc()
