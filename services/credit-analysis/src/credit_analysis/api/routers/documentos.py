"""Rota de upload e processamento de documento."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

import structlog
from fastapi import APIRouter, File, Form, UploadFile, status
from fastapi import Path as PathParam

from credit_analysis.api.deps import ProcessarDocumentoDep
from credit_analysis.api.observabilidade import registrar_processamento
from credit_analysis.api.schemas import DocumentoProcessadoResponse, ErroResponse
from credit_analysis.application.use_cases.processar_documento import (
    ComandoProcessarDocumento,
)
from credit_analysis.domain.enums import TipoDocumento
from credit_analysis.domain.exceptions import ValorInvalido
from credit_analysis.infrastructure.ocr.documentos import (
    EXTENSOES_IMAGEM,
    ErroLeituraDocumento,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/analises", tags=["Documentos"])


class ArquivoGrandeDemais(ValorInvalido):
    """Upload acima do teto configurado."""

    codigo = "arquivo_grande_demais"


# Teto de tamanho. Extrato anual escaneado passa de 20MB; 32MB da folga sem
# permitir que um upload unico consuma memoria de forma desproporcional.
TAMANHO_MAXIMO_BYTES = 32 * 1024 * 1024

# Bloco de leitura no streaming para disco. Ler `await arquivo.read()` inteiro
# carregaria os 32MB em memoria por requisicao concorrente.
BLOCO = 1024 * 1024

EXTENSOES_ACEITAS = frozenset({".pdf"}) | EXTENSOES_IMAGEM

_RESPOSTAS: dict[int | str, dict[str, Any]] = {
    404: {"model": ErroResponse, "description": "Analise nao encontrada"},
    409: {"model": ErroResponse, "description": "Analise ja processada"},
    413: {"model": ErroResponse, "description": "Arquivo grande demais"},
    422: {"model": ErroResponse, "description": "Arquivo invalido ou ilegivel"},
}


@router.post(
    "/{analise_id}/documentos",
    response_model=DocumentoProcessadoResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Enviar documento para extracao",
    responses=_RESPOSTAS,
)
async def enviar_documento(
    caso: ProcessarDocumentoDep,
    analise_id: Annotated[UUID, PathParam(description="Identificador da analise")],
    tipo: Annotated[TipoDocumento, Form(description="Tipo do documento enviado")],
    arquivo: Annotated[UploadFile, File(description="PDF ou imagem do documento")],
) -> DocumentoProcessadoResponse:
    """Extrai os dados de um documento e anexa a analise.

    O arquivo vai para um diretorio temporario e e apagado ao fim do
    processamento: o servico nao e repositorio de documento. A guarda dos
    originais (exigida pela POL-006 secao 5, por 5 anos) e responsabilidade de
    um bucket versionado com criptografia, que entra na Camada 6 junto com o S3.
    """
    sufixo = Path(arquivo.filename or "").suffix.lower()
    if sufixo not in EXTENSOES_ACEITAS:
        raise ValorInvalido(
            f"Extensao '{sufixo or '(ausente)'}' nao aceita. Envie {sorted(EXTENSOES_ACEITAS)}"
        )

    # Diretorio temporario proprio, e nome gerado por nos: usar o nome enviado
    # pelo cliente como caminho abriria path traversal ("../../etc/passwd").
    with tempfile.TemporaryDirectory(prefix="credit-doc-") as pasta:
        destino = Path(pasta) / f"upload{sufixo}"
        tamanho = await _gravar_com_limite(arquivo, destino)

        logger.info(
            "documento.recebido",
            analise_id=str(analise_id),
            tipo=tipo.value,
            bytes=tamanho,
            extensao=sufixo,
        )

        try:
            resultado = await caso.executar(
                ComandoProcessarDocumento(analise_id=analise_id, caminho=destino, tipo=tipo)
            )
        except ErroLeituraDocumento as exc:
            # Arquivo corrompido e erro do cliente (422), nao falha do servico.
            raise ValorInvalido(str(exc)) from exc

    registrar_processamento(resultado)
    return DocumentoProcessadoResponse.de_dominio(resultado)


async def _gravar_com_limite(arquivo: UploadFile, destino: Path) -> int:
    """Grava o upload em disco em blocos, abortando se passar do teto.

    Checar `arquivo.size` antes nao basta: o header `Content-Length` e informado
    pelo cliente e pode mentir. O limite tem que ser aplicado sobre o que
    realmente chega.

    ## As escritas vao para uma thread, e nao para o event loop

    A versao anterior chamava `saida.write(bloco)` direto dentro da corrotina. O
    lint de seguranca do outro servico apontou isso (regra ASYNC), e o apontamento
    esta correto: escrita em disco e chamada bloqueante, e enquanto ela roda **o
    event loop inteiro para**. Num upload de 10MB isso significa segurar todas as
    outras requisicoes do processo durante a gravacao.

    O padrao aqui e o que importa mais que a magnitude: `async def` cria a
    expectativa de que a funcao cede controle, e I/O sincrono dentro dela quebra
    essa expectativa em silencio. `asyncio.to_thread` move a operacao para o pool
    de threads, e o loop segue atendendo.
    """
    total = 0
    saida = await asyncio.to_thread(destino.open, "wb")
    try:
        while bloco := await arquivo.read(BLOCO):
            total += len(bloco)
            if total > TAMANHO_MAXIMO_BYTES:
                raise ArquivoGrandeDemais(
                    f"Arquivo excede o limite de {TAMANHO_MAXIMO_BYTES // (1024 * 1024)}MB"
                )
            await asyncio.to_thread(saida.write, bloco)
    except ArquivoGrandeDemais:
        # Fecha antes de remover: no Windows, apagar arquivo com handle aberto
        # levanta PermissionError, e o parcial ficaria em disco.
        await asyncio.to_thread(saida.close)
        await asyncio.to_thread(destino.unlink, True)
        raise
    finally:
        # Idempotente: fechar duas vezes nao levanta.
        await asyncio.to_thread(saida.close)

    if total == 0:
        raise ValorInvalido("Arquivo vazio")

    return total
