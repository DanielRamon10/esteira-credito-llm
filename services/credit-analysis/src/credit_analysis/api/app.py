"""Application factory da API.

`criar_app()` recebe os adapters por parametro. Isso e o que permite ao teste
de integracao subir a aplicacao inteira com um bureau deterministico e um
repositorio limpo, sem monkeypatch e sem variavel global.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, Response

from credit_analysis.api.errors import registrar_handlers
from credit_analysis.api.routers import analises, documentos, health, politicas
from credit_analysis.application.ports import (
    ConsultaBureau,
    ModeloLinguagem,
    MotorOCR,
    RepositorioAnalises,
)
from credit_analysis.config import ProvedorLLM, Settings, get_settings
from credit_analysis.infrastructure.bureau import BureauStub
from credit_analysis.infrastructure.llm.anthropic_adapter import LLMAnthropic, LLMFake
from credit_analysis.infrastructure.llm.ollama_adapter import (
    LLMOllama,
    modelos_instalados,
    ollama_disponivel,
)
from credit_analysis.infrastructure.logging import configurar_logging
from credit_analysis.infrastructure.ocr.escalonamento import MotorOCRComEscalonamento
from credit_analysis.infrastructure.ocr.tesseract import OCRTesseract, localizar_binario
from credit_analysis.infrastructure.ocr.vision import OCRClaudeVision
from credit_analysis.infrastructure.rag.embeddings import EmbedderFastEmbed
from credit_analysis.infrastructure.rag.pgvector_store import VectorStorePgVector, criar_pool
from credit_analysis.infrastructure.rag.retriever import RetrieverHibrido
from credit_analysis.infrastructure.repositories.memoria import RepositorioAnalisesMemoria

logger = structlog.get_logger(__name__)

CABECALHO_CORRELACAO = "X-Request-ID"


def criar_app(
    settings: Settings | None = None,
    repositorio: RepositorioAnalises | None = None,
    bureau: ConsultaBureau | None = None,
    retriever: RetrieverHibrido | None = None,
    llm: ModeloLinguagem | None = None,
    motor_ocr: MotorOCR | None = None,
) -> FastAPI:
    """Monta a aplicacao. Adapters omitidos caem no default de desenvolvimento."""
    settings = settings or get_settings()
    configurar_logging(nivel=settings.nivel_log, formato_json=settings.log_json)

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

        yield

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

    app.state.settings = settings
    app.state.repositorio = repositorio or RepositorioAnalisesMemoria()
    app.state.bureau = bureau or BureauStub()
    app.state.retriever = retriever  # pode virar pgvector no lifespan
    app.state.motor_ocr = motor_ocr or _montar_ocr(settings)
    app.state.llm = llm or _montar_llm(settings)

    @app.middleware("http")
    async def correlacao_e_log(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Propaga um request id e loga cada requisicao com ele.

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

        response: Response = await call_next(request)
        response.headers[CABECALHO_CORRELACAO] = request_id

        logger.info("http.requisicao", status=response.status_code)
        return response

    registrar_handlers(app)

    app.include_router(health.router)
    app.include_router(analises.router, prefix=settings.prefixo_api)
    app.include_router(politicas.router, prefix=settings.prefixo_api)
    app.include_router(documentos.router, prefix=settings.prefixo_api)

    return app


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


# Instancia usada pelo Uvicorn: `uvicorn credit_analysis.api.app:app`
app = criar_app()
