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
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from kyc_compliance.api.routers import health, triagens
from kyc_compliance.application.ports import RepositorioListas, RepositorioTriagens
from kyc_compliance.config import Settings, get_settings
from kyc_compliance.infrastructure.listas import ListasDeArquivo
from kyc_compliance.infrastructure.logging import configurar_logging
from kyc_compliance.infrastructure.repositories.memoria import RepositorioTriagensMemoria

logger = structlog.get_logger(__name__)

CABECALHO_CORRELACAO = "X-Request-ID"


def criar_app(
    settings: Settings | None = None,
    listas: RepositorioListas | None = None,
    repositorio: RepositorioTriagens | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configurar_logging(nivel=settings.nivel_log, formato_json=settings.log_json)

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
        response: Response = await call_next(request)
        duracao = time.perf_counter() - inicio

        response.headers[CABECALHO_CORRELACAO] = request_id
        logger.info("http.requisicao", status=response.status_code, duracao_ms=int(duracao * 1000))
        return response

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

    app.include_router(health.router)
    app.include_router(triagens.router, prefix=settings.prefixo_api)

    return app


app = criar_app()
