"""Application factory do customer-support."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from plataforma import seguranca
from plataforma.logging import configurar_logging
from plataforma.metricas import rotulo_de_rota

from customer_support.api.routers import atendimentos, health
from customer_support.api.routers import metricas as rota_metricas
from customer_support.application.ports import BaseDeConhecimento, ModeloLinguagem
from customer_support.application.use_cases.atender import Atender
from customer_support.config import ProvedorLLM, Settings, get_settings
from customer_support.infrastructure import metricas
from customer_support.infrastructure.conhecimento import ConhecimentoEmArquivos

logger = structlog.get_logger(__name__)

CABECALHO_CORRELACAO = "X-Request-ID"


_observadores_ligados = False


def _ligar_observadores() -> None:
    """Liga o gancho de deteccao de injecao da plataforma a metrica deste servico.

    Idempotente: `criar_app` e chamada dezenas de vezes na suite, e sem a guarda cada
    chamada empilharia mais um observador — o contador passaria a incrementar N vezes
    por evento e o painel mentiria para cima.
    """
    global _observadores_ligados
    if _observadores_ligados:
        return
    seguranca.registrar_observador(_medir_injecao)
    _observadores_ligados = True


def _medir_injecao(superficie: str, categoria: str) -> None:
    """Traduz o gancho da plataforma na metrica deste servico.

    A `superficie` e ignorada aqui de proposito: neste servico ha apenas uma —
    a mensagem do proprio cliente. Registra-la como label criaria uma dimensao com um
    unico valor possivel, que nao separa nada e ocupa espaco na serie.
    """
    metricas.injecao_detectada.labels(categoria=categoria).inc()


def criar_app(
    settings: Settings | None = None,
    conhecimento: BaseDeConhecimento | None = None,
    llm: ModeloLinguagem | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configurar_logging(nivel=settings.nivel_log, formato_json=settings.log_json)
    metricas.http.publicar_info(versao=settings.versao, ambiente=settings.ambiente.value)
    _ligar_observadores()

    base = conhecimento or ConhecimentoEmArquivos(settings.diretorio_conhecimento)
    modelo = llm if llm is not None else _montar_llm(settings)

    @asynccontextmanager
    async def lifespan(app_: FastAPI) -> AsyncGenerator[None]:
        logger.info(
            "servico.iniciando",
            servico=settings.nome_servico,
            versao=settings.versao,
            ambiente=settings.ambiente.value,
            artigos=base.total,
            llm=modelo.identificacao if modelo else "artigo",
        )
        yield
        logger.info("servico.encerrando", servico=settings.nome_servico)

    app = FastAPI(
        title="Customer Support API",
        description=(
            "Atendimento ao cliente sobre produtos de credito, com roteamento "
            "deterministico e fronteira de divulgacao."
        ),
        version=settings.versao,
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_habilitados else None,
        redoc_url="/redoc" if settings.docs_habilitados else None,
        openapi_url="/openapi.json" if settings.docs_habilitados else None,
    )

    app.state.settings = settings
    app.state.conhecimento = base
    app.state.llm = modelo
    app.state.caso_atender = Atender(
        conhecimento=base, llm=modelo, artigos_no_prompt=settings.artigos_no_prompt
    )

    @app.middleware("http")
    async def correlacao_e_log(request: Request, call_next):  # type: ignore[no-untyped-def]
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

    @app.exception_handler(RequestValidationError)
    async def validacao(_: Request, exc: RequestValidationError) -> JSONResponse:
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
    app.include_router(atendimentos.router, prefix=settings.prefixo_api)

    return app


def _montar_llm(settings: Settings) -> ModeloLinguagem | None:
    """Escolhe o adapter, ou devolve None para responder com o artigo.

    **Nao ha fake de producao aqui**, e a diferenca em relacao ao `credit-analysis` e
    proposital. La o fake produz um parecer sintetico que um analista consegue
    reconhecer como tal. Aqui o texto vai para o cliente: responder com prosa
    sintetica seria pior que responder o artigo cru, que ao menos foi revisado por
    gente.
    """
    if settings.provedor_llm is ProvedorLLM.ARTIGO:
        logger.info("llm.desabilitado", motivo="provedor_llm=artigo")
        return None

    from plataforma.llm import criar_chat_ollama, ollama_disponivel  # noqa: F401

    if not ollama_disponivel(settings.ollama_endpoint):
        if settings.provedor_llm is ProvedorLLM.OLLAMA:
            raise RuntimeError(
                f"SUP_PROVEDOR_LLM=ollama mas o daemon nao responde em "
                f"{settings.ollama_endpoint}. Rode `ollama serve`."
            )
        logger.warning(
            "llm.indisponivel", efeito="respostas virao do texto do artigo, sem reescrita"
        )
        return None

    from plataforma.llm import LLMOllama

    return LLMOllama(
        modelo=settings.modelo_ollama,
        endpoint=settings.ollama_endpoint,
        timeout_segundos=settings.ollama_timeout_segundos,
        # `format="json"` fica DESLIGADO: aqui a saida e prosa para cliente, nao um
        # objeto estruturado. Forcar JSON produziria uma resposta que precisa ser
        # desembrulhada, e qualquer falha no desembrulho apareceria como texto cru.
        forcar_json=False,
    )

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
