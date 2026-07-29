"""Traducao de erros de dominio para respostas HTTP.

Handlers centralizados em vez de try/except em cada rota. O router fica limpo
e nenhuma excecao de negocio escapa como 500 generico.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from credit_analysis.api.routers.documentos import ArquivoGrandeDemais
from credit_analysis.api.schemas import ErroResponse
from credit_analysis.domain.exceptions import (
    AnaliseNaoEncontrada,
    DadosInsuficientes,
    ErroDominio,
    RecursoIndisponivel,
    TransicaoInvalida,
    ValorInvalido,
)

logger = structlog.get_logger(__name__)

# Cada erro de dominio tem um status HTTP proprio. Mapear como dado deixa a
# tabela inteira visivel em um lugar so.
_STATUS_POR_ERRO: dict[type[ErroDominio], int] = {
    AnaliseNaoEncontrada: status.HTTP_404_NOT_FOUND,
    ValorInvalido: status.HTTP_422_UNPROCESSABLE_CONTENT,
    DadosInsuficientes: status.HTTP_422_UNPROCESSABLE_CONTENT,
    TransicaoInvalida: status.HTTP_409_CONFLICT,
    RecursoIndisponivel: status.HTTP_503_SERVICE_UNAVAILABLE,
    # Precede ValorInvalido na busca pela MRO por ser subclasse dela.
    ArquivoGrandeDemais: status.HTTP_413_CONTENT_TOO_LARGE,
}


def _status_para(exc: ErroDominio) -> int:
    # Percorre a MRO para que subclasses herdem o status do pai.
    for classe in type(exc).__mro__:
        if classe in _STATUS_POR_ERRO:
            return _STATUS_POR_ERRO[classe]
    return status.HTTP_400_BAD_REQUEST


def registrar_handlers(app: FastAPI) -> None:
    """Instala os exception handlers na aplicacao."""

    @app.exception_handler(ErroDominio)
    async def _dominio(request: Request, exc: ErroDominio) -> JSONResponse:
        http_status = _status_para(exc)
        logger.warning(
            "erro.dominio",
            codigo=exc.codigo,
            mensagem=str(exc),
            path=request.url.path,
            status=http_status,
        )
        return JSONResponse(
            status_code=http_status,
            content=ErroResponse(codigo=exc.codigo, mensagem=str(exc)).model_dump(),
        )

    @app.exception_handler(RequestValidationError)
    async def _validacao(request: Request, exc: RequestValidationError) -> JSONResponse:
        detalhes = [
            {
                "campo": ".".join(str(p) for p in erro["loc"][1:]),
                "erro": erro["msg"],
                "tipo": erro["type"],
            }
            for erro in exc.errors()
        ]
        logger.info("erro.validacao", path=request.url.path, campos=len(detalhes))
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=ErroResponse(
                codigo="payload_invalido",
                mensagem="A requisicao nao passou na validacao de schema",
                detalhes=detalhes,
            ).model_dump(),
        )

    @app.exception_handler(Exception)
    async def _inesperado(request: Request, exc: Exception) -> JSONResponse:
        # Loga o stack completo mas nao devolve nada dele ao cliente: mensagem
        # de excecao vaza caminho de arquivo, query e as vezes credencial.
        logger.error("erro.inesperado", path=request.url.path, erro=str(exc), exc_info=True)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=ErroResponse(
                codigo="erro_interno",
                mensagem="Erro interno ao processar a requisicao",
            ).model_dump(),
        )
