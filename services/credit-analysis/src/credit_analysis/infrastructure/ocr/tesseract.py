"""Adapter Tesseract do port `MotorOCR`.

Rodando local: sem chamada de rede, sem custo por pagina, e o documento — que
contem dado pessoal sob a LGPD — nao sai da infraestrutura. Para holerite e
extrato gerados por sistema, que e a maioria do volume, a acuracia e alta.

A confianca vem do proprio Tesseract, via `image_to_data`, que reporta um score
por palavra. A media ponderada desses scores e o que alimenta a decisao de
escalonamento — sem ela o motor seria uma caixa que devolve texto sem dizer o
quanto acredita nele.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from functools import cached_property
from pathlib import Path

import pytesseract
import structlog
from PIL import Image

from credit_analysis.domain.documento import ImagemDocumento, ResultadoOCR
from credit_analysis.domain.value_objects import Percentual
from credit_analysis.infrastructure.ocr.preprocessamento import preprocessar

logger = structlog.get_logger(__name__)

IDIOMA_PADRAO = "por"

# PSM 6 = "bloco uniforme de texto". Holerite e extrato sao tabelas em bloco
# unico; o modo automatico (3) tenta detectar colunas e frequentemente parte a
# tabela, associando valor a linha errada.
PSM_PADRAO = 6

# Palavras com confianca abaixo disso nao entram na media. O Tesseract atribui
# score baixo a ruido que ele mesmo reconheceu como improvavel; incluir isso
# puxaria a media para baixo por causa de lixo que sera descartado de todo jeito.
CONFIANCA_MINIMA_PALAVRA = 30.0

# Caminhos usuais no Windows. Em Linux o binario fica no PATH e nada disso e
# consultado.
_CAMINHOS_WINDOWS = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
)


class TesseractIndisponivel(RuntimeError):
    """O binario do Tesseract nao foi encontrado."""


def localizar_binario() -> str | None:
    """Acha o executavel do Tesseract.

    Ordem: variavel de ambiente, PATH, caminhos usuais do Windows. A variavel
    vem primeiro para que o operador possa sobrepor sem mexer no codigo.
    """
    if (explicito := os.getenv("TESSERACT_CMD")) and Path(explicito).is_file():
        return explicito

    if encontrado := shutil.which("tesseract"):
        return encontrado

    for caminho in _CAMINHOS_WINDOWS:
        if Path(caminho).is_file():
            return caminho

    return None


class OCRTesseract:
    """OCR local via Tesseract."""

    def __init__(
        self,
        idioma: str = IDIOMA_PADRAO,
        psm: int = PSM_PADRAO,
        preprocessar_imagem: bool = True,
    ) -> None:
        self._idioma = idioma
        self._psm = psm
        self._preprocessar = preprocessar_imagem

    @cached_property
    def _binario(self) -> str:
        caminho = localizar_binario()
        if caminho is None:
            raise TesseractIndisponivel(
                "Tesseract nao encontrado. Instale com "
                "`winget install UB-Mannheim.TesseractOCR` (Windows) ou "
                "`apt-get install tesseract-ocr tesseract-ocr-por` (Debian), "
                "ou aponte TESSERACT_CMD para o binario."
            )
        pytesseract.pytesseract.tesseract_cmd = caminho
        return caminho

    @property
    def identificacao(self) -> str:
        return f"tesseract:{self._idioma}"

    @property
    def custo_relativo(self) -> int:
        return 1  # local e gratuito

    def disponivel(self) -> bool:
        return localizar_binario() is not None

    async def extrair(self, imagem: ImagemDocumento) -> ResultadoOCR:
        # O Tesseract e CPU-bound e bloqueante; sem o executor ele travaria o
        # event loop por segundos e a API pararia de responder a tudo.
        return await asyncio.to_thread(self._extrair_sincrono, imagem)

    def _extrair_sincrono(self, imagem: ImagemDocumento) -> ResultadoOCR:
        self._binario  # noqa: B018 — dispara a validacao de disponibilidade

        correcoes: tuple[str, ...] = ()
        matriz = imagem

        if self._preprocessar:
            resultado = preprocessar(imagem)
            matriz = resultado.imagem
            correcoes = resultado.correcoes

        pil = Image.fromarray(matriz)
        config = f"--psm {self._psm}"

        texto = pytesseract.image_to_string(pil, lang=self._idioma, config=config)
        confianca, palavras = self._medir_confianca(pil, config)

        logger.info(
            "ocr.tesseract",
            confianca=float(confianca.valor),
            palavras=palavras,
            caracteres=len(texto.strip()),
            correcoes=list(correcoes),
        )

        return ResultadoOCR(
            texto=texto,
            confianca=confianca,
            motor=self.identificacao,
            palavras_reconhecidas=palavras,
            correcoes_aplicadas=correcoes,
        )

    def _medir_confianca(self, pil: Image.Image, config: str) -> tuple[Percentual, int]:
        """Confianca media ponderada pelo tamanho da palavra.

        Ponderar pelo comprimento evita que uma enxurrada de fragmentos de um
        caractere domine a media: "8.500,00" reconhecido com 95% deve pesar
        mais que um "|" espurio reconhecido com 40%.
        """
        dados = pytesseract.image_to_data(
            pil, lang=self._idioma, config=config, output_type=pytesseract.Output.DICT
        )

        soma_pesos = 0.0
        soma_ponderada = 0.0
        palavras = 0

        for texto_palavra, confianca_bruta in zip(dados["text"], dados["conf"], strict=True):
            limpo = (texto_palavra or "").strip()
            if not limpo:
                continue

            try:
                confianca = float(confianca_bruta)
            except (TypeError, ValueError):
                continue

            # -1 marca bloco sem texto reconhecido.
            if confianca < CONFIANCA_MINIMA_PALAVRA:
                continue

            peso = float(len(limpo))
            soma_ponderada += confianca * peso
            soma_pesos += peso
            palavras += 1

        if soma_pesos == 0:
            return Percentual.de(0), 0

        return Percentual.de(round(soma_ponderada / soma_pesos, 2)), palavras
