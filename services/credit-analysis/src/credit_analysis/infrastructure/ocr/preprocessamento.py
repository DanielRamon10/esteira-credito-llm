"""Pre-processamento de imagem para OCR.

O Tesseract nao "ve" o documento: ele segmenta linhas e caracteres a partir de
uma imagem binaria. Entao a qualidade do OCR e determinada, em boa parte,
antes de o OCR rodar. Quatro correcoes cobrem a maioria dos casos reais:

1. **Escala** — abaixo de ~200 DPI o tracado do caractere fica com poucos
   pixels e o reconhecimento degrada rapido. Ampliar nao recupera informacao,
   mas da ao segmentador area suficiente para trabalhar.
2. **Deskew** — o Tesseract tolera pouco mais de 1-2 graus de rotacao. Foto de
   documento na mesa passa disso com facilidade.
3. **Denoise** — ruido de sensor vira caractere fantasma.
4. **Binarizacao adaptativa** — iluminacao irregular (sombra da propria mao ao
   fotografar) quebra qualquer limiar global.

A ordem importa: deskew antes de binarizar, porque a rotacao interpola pixels e
geraria tons intermediarios num material ja binario.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import cv2
import numpy as np
import numpy.typing as npt
import structlog

logger = structlog.get_logger(__name__)

Imagem = npt.NDArray[np.uint8]

# Altura minima em pixels para uma pagina A4. Abaixo disso ampliamos.
# ~1650px corresponde a 200 DPI, o minimo que a POL-002 secao 3.2 exige.
ALTURA_MINIMA = 1600

# Nao amplia alem disso: ampliar demais nao adiciona informacao e o custo de
# processamento (e de tokens, num adapter de visao) cresce com a area.
FATOR_MAXIMO_AMPLIACAO = 3.0

# Angulos acima disso sao tratados como erro de deteccao, nao inclinacao. Um
# documento de verdade nao chega a 15 graus; se a estimativa apontar isso, o
# mais provavel e que a heuristica pegou uma borda ou tabela.
ANGULO_MAXIMO_CORRECAO = 15.0


@dataclass(frozen=True, slots=True)
class ResultadoPreprocessamento:
    """Imagem tratada e o que foi feito nela.

    O registro das correcoes vai para o log e para o parecer: quando um caso
    vai para revisao humana, o analista precisa saber que a imagem foi
    rotacionada 4 graus e ampliada 2x, porque isso muda o quanto ele confia no
    texto extraido.
    """

    imagem: Imagem
    angulo_corrigido: float = 0.0
    fator_escala: float = 1.0
    correcoes: tuple[str, ...] = ()

    @property
    def houve_correcao(self) -> bool:
        return bool(self.correcoes)


def estimar_inclinacao(cinza: Imagem) -> float:
    """Estima a inclinacao do texto em graus.

    Usa `minAreaRect` sobre os pixels de texto: o retangulo de menor area que
    envolve todo o conteudo tende a se alinhar com a direcao das linhas.
    E mais barato que transformada de Hough e suficiente para documento
    estruturado, onde as linhas de texto dominam a imagem.
    """
    # Inverte para que texto seja "primeiro plano" (valor alto), como o
    # findNonZero espera.
    _, binaria = cv2.threshold(cinza, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)

    coordenadas = cv2.findNonZero(binaria)
    if coordenadas is None or len(coordenadas) < 50:
        return 0.0

    angulo = float(cv2.minAreaRect(coordenadas)[-1])

    # Normaliza para [-45, 45], a faixa onde "inclinacao" faz sentido: 89 graus
    # e -1 grau, nao 89.
    #
    # O modulo antes da comparacao e deliberado. A convencao de angulo do
    # `minAreaRect` mudou entre versoes do OpenCV — a 4.x devolvia em [0, 90) e
    # a 5.x devolve valores negativos —, e um `if angulo > 45: angulo -= 90`
    # sozinho classificava documento perfeitamente reto como inclinado em -90
    # graus. Com `% 90` o resultado e o mesmo nas duas convencoes.
    angulo %= 90
    if angulo > 45:
        angulo -= 90

    return angulo


def corrigir_inclinacao(cinza: Imagem, angulo: float) -> Imagem:
    """Rotaciona a imagem em torno do centro, expandindo a moldura.

    `borderValue=255` preenche com branco: preencher com preto (o default)
    criaria bordas escuras que o binarizador trataria como conteudo.
    """
    altura, largura = cinza.shape[:2]
    centro = (largura / 2, altura / 2)
    matriz = cv2.getRotationMatrix2D(centro, angulo, 1.0)

    # Recalcula a moldura para nao cortar canto ao rotacionar.
    cos, sen = abs(matriz[0, 0]), abs(matriz[0, 1])
    nova_largura = int(altura * sen + largura * cos)
    nova_altura = int(altura * cos + largura * sen)
    matriz[0, 2] += nova_largura / 2 - centro[0]
    matriz[1, 2] += nova_altura / 2 - centro[1]

    # O OpenCV nao preserva o dtype na assinatura estatica; em runtime a
    # entrada uint8 sai uint8, entao o cast declara o que ja e verdade.
    return cast(
        Imagem,
        cv2.warpAffine(
            cinza,
            matriz,
            (nova_largura, nova_altura),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=255,
        ),
    )


def _ampliar_se_pequena(cinza: Imagem) -> tuple[Imagem, float]:
    altura = cinza.shape[0]
    if altura >= ALTURA_MINIMA:
        return cinza, 1.0

    fator = min(ALTURA_MINIMA / altura, FATOR_MAXIMO_AMPLIACAO)
    # INTER_CUBIC preserva melhor o tracado do caractere que INTER_LINEAR ao
    # ampliar.
    ampliada = cv2.resize(cinza, None, fx=fator, fy=fator, interpolation=cv2.INTER_CUBIC)
    return cast(Imagem, ampliada), fator


def preprocessar(
    imagem: Imagem,
    *,
    corrigir_rotacao: bool = True,
    remover_ruido: bool = True,
    binarizar: bool = True,
) -> ResultadoPreprocessamento:
    """Prepara a imagem para OCR, registrando o que foi corrigido.

    Os passos sao opcionais porque documento gerado digitalmente (PDF
    rasterizado, print de tela) ja chega limpo — e binarizar o que ja e binario
    so introduz artefato.
    """
    correcoes: list[str] = []

    cinza: Imagem = (
        imagem if imagem.ndim == 2 else cast(Imagem, cv2.cvtColor(imagem, cv2.COLOR_BGR2GRAY))
    )

    cinza, fator = _ampliar_se_pequena(cinza)
    if fator != 1.0:
        correcoes.append(f"ampliada {fator:.2f}x")

    angulo = 0.0
    if corrigir_rotacao:
        estimado = estimar_inclinacao(cinza)
        # Abaixo de 0.3 grau nao vale interpolar a imagem inteira: o ganho e
        # nulo e a interpolacao suaviza o tracado.
        if 0.3 < abs(estimado) <= ANGULO_MAXIMO_CORRECAO:
            cinza = corrigir_inclinacao(cinza, estimado)
            angulo = estimado
            correcoes.append(f"rotacao corrigida {estimado:+.2f} graus")
        elif abs(estimado) > ANGULO_MAXIMO_CORRECAO:
            logger.warning("ocr.inclinacao_implausivel", angulo=round(estimado, 2))

    if remover_ruido:
        # fastNlMeansDenoising preserva borda melhor que blur gaussiano, o que
        # importa quando a borda e o contorno do caractere.
        cinza = cast(
            Imagem,
            cv2.fastNlMeansDenoising(cinza, None, h=10, templateWindowSize=7, searchWindowSize=21),
        )
        correcoes.append("ruido reduzido")

    if binarizar:
        # Adaptativo, nao Otsu global: sombra num canto da foto derruba
        # qualquer limiar unico para a imagem inteira.
        cinza = cast(
            Imagem,
            cv2.adaptiveThreshold(
                cinza,
                255,
                cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY,
                blockSize=31,
                C=15,
            ),
        )
        correcoes.append("binarizada (adaptativa)")

    return ResultadoPreprocessamento(
        imagem=cinza,
        angulo_corrigido=angulo,
        fator_escala=fator,
        correcoes=tuple(correcoes),
    )
