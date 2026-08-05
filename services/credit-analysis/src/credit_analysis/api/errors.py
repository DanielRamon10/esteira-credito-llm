"""Traducao de erros de dominio para respostas HTTP.

Handlers centralizados em vez de try/except em cada rota. O router fica limpo
e nenhuma excecao de negocio escapa como 500 generico.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from plataforma import autenticacao as auth

from credit_analysis.api.routers.documentos import ArquivoGrandeDemais
from credit_analysis.api.schemas import ErroResponse
from credit_analysis.domain.exceptions import (
    AnaliseNaoEncontrada,
    ChaveDeIdempotenciaAusente,
    ChaveDeIdempotenciaReusada,
    DadosInsuficientes,
    ErroDominio,
    PedidoEmAndamento,
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
    # Idempotencia (Camada 11). Os tres codigos sao diferentes de proposito: 400 para chave ausente
    # (o cliente nao mandou o que a rota exige), 422 para chave reusada com pedido diferente (o que
    # ele mandou e incoerente), 409 para pedido em andamento (estado, e temporario).
    ChaveDeIdempotenciaAusente: status.HTTP_400_BAD_REQUEST,
    ChaveDeIdempotenciaReusada: status.HTTP_422_UNPROCESSABLE_CONTENT,
    PedidoEmAndamento: status.HTTP_409_CONFLICT,
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

    @app.exception_handler(auth.EscopoInsuficiente)
    async def _escopo(request: Request, exc: auth.EscopoInsuficiente) -> JSONResponse:
        """403, nunca 401 — e a ordem deste handler importa.

        `EscopoInsuficiente` NAO herda de `TokenInvalido` justamente para nao cair no
        handler abaixo. As duas respostas dizem coisas diferentes ao cliente:

            401  "suas credenciais nao servem, tente outras"
            403  "suas credenciais servem e nao bastam"

        Devolver 401 aqui manda um cliente correto reautenticar num laco que nunca resolve,
        e esconde de quem opera que o problema e de permissao e nao de credencial.

        A mensagem **nao** diz qual escopo falta. Enumerar escopos para quem nao os tem e
        entregar o mapa de permissoes do servico — a mesma logica da fronteira de
        divulgacao do `customer-support`. Quem opera ve o escopo no log.
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

    @app.exception_handler(auth.ErroDeAutenticacao)
    async def _autenticacao(request: Request, exc: auth.ErroDeAutenticacao) -> JSONResponse:
        """401 com `WWW-Authenticate`, como manda a RFC 6750 secao 3.

        Sem esse cabecalho o cliente nao tem como descobrir **como** se autenticar, e
        bibliotecas HTTP que renovam token automaticamente nao disparam a renovacao.

        O `error` no cabecalho e um dominio fechado do proprio RFC (`invalid_token`); o
        `motivo` interno (expirado, audiencia_incorreta, ...) fica no log e na metrica, nao
        na resposta. Dizer ao cliente que a audiencia estava errada confirma que existe um
        servico com outra audiencia — informacao util para quem esta mapeando a superficie.
        """
        logger.warning(
            "auth.negado",
            motivo=exc.motivo,
            path=request.url.path,
            # Sem o token, nem truncado: um prefixo de JWT ja revela emissor e audiencia,
            # e log e o lugar de onde credencial vaza para print de Slack.
        )
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={
                "WWW-Authenticate": (
                    'Bearer realm="credit-analysis", error="invalid_token"'
                    if not isinstance(exc, auth.TokenAusente)
                    # Token ausente nao e token invalido: sem `error`, o cliente sabe que
                    # precisa apresentar credencial em vez de trocar a que tem.
                    else 'Bearer realm="credit-analysis"'
                )
            },
            content=ErroResponse(
                codigo="nao_autenticado",
                mensagem="Credencial ausente ou invalida.",
            ).model_dump(),
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
