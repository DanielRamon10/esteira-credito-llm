"""Leitura de arquivos de documento: PDF e imagem.

A decisao mais importante deste modulo e **nao rodar OCR quando nao precisa**.

Boa parte dos documentos financeiros chega como PDF gerado digitalmente
(internet banking, folha de pagamento emitida por sistema), com camada de texto
embutida. Nesse caso extrair o texto direto e melhor em todas as dimensoes:
exato em vez de aproximado, milissegundos em vez de segundos, e sem risco de
trocar 8 por B. Rasterizar esse PDF para depois "reconhecer" um texto que ja
estava disponivel e jogar precisao fora.

O modulo detecta o caso e escolhe o caminho. OCR entra so quando o PDF e um
scan (imagem dentro de PDF) ou quando o arquivo e imagem.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast

import cv2
import fitz  # PyMuPDF
import numpy as np
import numpy.typing as npt
import structlog

logger = structlog.get_logger(__name__)

Imagem = npt.NDArray[np.uint8]

# Resolucao de rasterizacao de PDF sem camada de texto. 200 DPI e o minimo da
# POL-002 secao 3.2; usamos 300 porque rasterizar e barato e mais resolucao
# ajuda o OCR num scan ruim.
DPI_RASTERIZACAO = 300

# Minimo de caracteres por pagina para considerar que existe camada de texto
# util. PDF escaneado costuma trazer alguns caracteres de lixo (marca d'agua,
# metadado), e um limiar de zero classificaria isso como texto valido.
MIN_CARACTERES_CAMADA_TEXTO = 120

# Limite de paginas processadas. Extrato anual tem dezenas de paginas; sem teto,
# um upload malicioso de PDF com 10.000 paginas viraria negacao de servico.
MAX_PAGINAS = 20

EXTENSOES_IMAGEM = frozenset({".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"})


class ErroLeituraDocumento(Exception):
    """Arquivo ilegivel, corrompido ou de tipo nao suportado."""


class OrigemTexto(StrEnum):
    """Como o texto foi obtido — determina o quanto confiar nele."""

    CAMADA_PDF = "camada_pdf"  # exato
    OCR = "ocr"  # aproximado


@dataclass(frozen=True, slots=True)
class PaginaDocumento:
    """Uma pagina pronta para o proximo passo do pipeline."""

    numero: int
    imagem: Imagem | None
    texto_embutido: str | None

    @property
    def tem_texto_embutido(self) -> bool:
        return bool(self.texto_embutido and self.texto_embutido.strip())


@dataclass(frozen=True, slots=True)
class DocumentoCarregado:
    """Resultado da leitura do arquivo, antes do OCR."""

    paginas: tuple[PaginaDocumento, ...]
    origem_sugerida: OrigemTexto
    total_paginas_no_arquivo: int

    @property
    def paginas_truncadas(self) -> int:
        return max(0, self.total_paginas_no_arquivo - len(self.paginas))

    @property
    def texto_embutido(self) -> str:
        return "\n\n".join(p.texto_embutido or "" for p in self.paginas).strip()


def carregar(caminho: Path) -> DocumentoCarregado:
    """Le um arquivo de documento, escolhendo entre camada de texto e OCR."""
    if not caminho.is_file():
        raise ErroLeituraDocumento(f"Arquivo nao encontrado: {caminho}")

    sufixo = caminho.suffix.lower()

    if sufixo == ".pdf":
        return _carregar_pdf(caminho)
    if sufixo in EXTENSOES_IMAGEM:
        return _carregar_imagem(caminho)

    raise ErroLeituraDocumento(
        f"Tipo nao suportado: {sufixo or '(sem extensao)'}. "
        f"Aceitos: .pdf e {sorted(EXTENSOES_IMAGEM)}"
    )


def _carregar_pdf(caminho: Path) -> DocumentoCarregado:
    try:
        documento = fitz.open(caminho)
    except Exception as exc:  # PyMuPDF levanta varios tipos
        raise ErroLeituraDocumento(f"PDF ilegivel: {exc}") from exc

    try:
        total = documento.page_count
        if total == 0:
            raise ErroLeituraDocumento("PDF sem paginas")

        quantidade = min(total, MAX_PAGINAS)
        if total > MAX_PAGINAS:
            logger.warning("documento.paginas_truncadas", total=total, processadas=quantidade)

        textos = [documento[i].get_text("text") or "" for i in range(quantidade)]
        caracteres = sum(len(t.strip()) for t in textos)
        tem_camada = caracteres >= MIN_CARACTERES_CAMADA_TEXTO * quantidade

        if tem_camada:
            logger.info("documento.camada_texto_detectada", paginas=quantidade, chars=caracteres)
            paginas = tuple(
                PaginaDocumento(numero=i + 1, imagem=None, texto_embutido=textos[i])
                for i in range(quantidade)
            )
            return DocumentoCarregado(paginas, OrigemTexto.CAMADA_PDF, total)

        logger.info("documento.sem_camada_texto", paginas=quantidade, chars=caracteres)
        paginas = tuple(
            PaginaDocumento(
                numero=i + 1,
                imagem=_rasterizar(documento[i]),
                texto_embutido=None,
            )
            for i in range(quantidade)
        )
        return DocumentoCarregado(paginas, OrigemTexto.OCR, total)
    finally:
        documento.close()


def _rasterizar(pagina: fitz.Page) -> Imagem:
    """Renderiza a pagina como imagem em tons de cinza."""
    matriz = fitz.Matrix(DPI_RASTERIZACAO / 72, DPI_RASTERIZACAO / 72)
    pixmap = pagina.get_pixmap(matrix=matriz, colorspace=fitz.csGRAY)

    matriz_np = np.frombuffer(pixmap.samples, dtype=np.uint8)
    return cast(Imagem, matriz_np.reshape(pixmap.height, pixmap.width).copy())


def _carregar_imagem(caminho: Path) -> DocumentoCarregado:
    # `imdecode` sobre bytes em vez de `imread` com o caminho: o imread do
    # OpenCV falha silenciosamente (devolve None) com caminho contendo
    # caractere nao-ASCII, que e comum em "Documentos/Projetos Pessoais".
    dados = np.frombuffer(caminho.read_bytes(), dtype=np.uint8)
    imagem = cast("Imagem | None", cv2.imdecode(dados, cv2.IMREAD_GRAYSCALE))

    if imagem is None:
        raise ErroLeituraDocumento(f"Imagem ilegivel ou corrompida: {caminho.name}")

    pagina = PaginaDocumento(numero=1, imagem=imagem, texto_embutido=None)
    return DocumentoCarregado((pagina,), OrigemTexto.OCR, 1)
