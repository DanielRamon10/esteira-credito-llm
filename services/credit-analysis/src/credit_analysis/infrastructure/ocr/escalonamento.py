"""Cadeia de motores de OCR com escalonamento.

Implementa o proprio port `MotorOCR`, entao quem consome nao sabe que ha uma
cadeia: e o padrao Composite. A politica e simples — tenta o motor mais barato,
e escala para o proximo se o resultado nao servir.

**Por que a decisao nao pode ser so a confianca.** A medicao em
`tests/eval/test_ocr_qualidade.py` mostrou dois problemas com um limiar global:

- *Falso negativo*: o extrato bancario limpo, com 24/24 lancamentos lidos
  corretamente, sai com 83,9% de confianca — abaixo do limiar de 85% da
  POL-002. Tabela densa de numeros monoespacados recebe score por palavra mais
  baixo que prosa, entao uma extracao perfeita seria mandada para revisao
  humana.
- *Falso positivo*: o holerite em baixa resolucao sai com 87,8% (acima do
  limiar) mas perde o CPF — 4 de 5 campos.

Ou seja: confianca alta nao garante campo extraido, e confianca baixa nao
implica extracao ruim. Por isso a cadeia aceita um **verificador de suficiencia**
que olha o texto e responde se ele serve para o proposito. A confianca entra
como sinal secundario, para o caso em que nao ha verificador.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import structlog

from credit_analysis.application.ports import MotorOCR
from credit_analysis.domain.documento import (
    CONFIANCA_MINIMA_AUTOMATICA,
    ImagemDocumento,
    QualidadeExtracao,
    ResultadoOCR,
)
from credit_analysis.domain.value_objects import Percentual

logger = structlog.get_logger(__name__)

# Assinatura do verificador: recebe o texto extraido e responde se ele contem o
# necessario. Um callable simples em vez de outro Protocol porque a unica coisa
# que varia e a pergunta "isto serve?".
Suficiencia = Callable[[str], bool]


@dataclass(frozen=True, slots=True)
class TentativaOCR:
    """Registro de uma passada por um motor da cadeia."""

    motor: str
    confianca: Percentual
    suficiente: bool
    escalou: bool


@dataclass(slots=True)
class MotorOCRComEscalonamento:
    """Cadeia de motores, do mais barato ao mais caro.

    Nao ordena a lista internamente: a ordem que o chamador passou e a ordem de
    tentativa. Reordenar por `custo_relativo` pareceria conveniente, mas
    esconderia do operador qual motor roda primeiro — e essa e exatamente a
    decisao que ele quer controlar.
    """

    motores: Sequence[MotorOCR]
    suficiencia: Suficiencia | None = None
    confianca_minima: Percentual = CONFIANCA_MINIMA_AUTOMATICA
    tentativas: list[TentativaOCR] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.motores:
            raise ValueError("A cadeia precisa de ao menos um motor de OCR")

    @property
    def identificacao(self) -> str:
        return "cadeia:" + ">".join(m.identificacao for m in self.motores)

    @property
    def custo_relativo(self) -> int:
        return min(m.custo_relativo for m in self.motores)

    async def extrair(self, imagem: ImagemDocumento) -> ResultadoOCR:
        self.tentativas = []
        melhor: ResultadoOCR | None = None

        for indice, motor in enumerate(self.motores):
            ultimo = indice == len(self.motores) - 1

            try:
                resultado = await motor.extrair(imagem)
            except Exception as exc:
                # Falha de um motor nao encerra a cadeia: e justamente o caso
                # em que escalar faz sentido (Tesseract ausente, API fora).
                logger.warning(
                    "ocr.motor_falhou",
                    motor=motor.identificacao,
                    erro=str(exc),
                    ultimo_da_cadeia=ultimo,
                )
                if ultimo and melhor is None:
                    raise
                continue

            suficiente = self._suficiente(resultado)
            self.tentativas.append(
                TentativaOCR(
                    motor=resultado.motor,
                    confianca=resultado.confianca,
                    suficiente=suficiente,
                    escalou=not suficiente and not ultimo,
                )
            )

            # Guarda o melhor visto: se nenhum motor satisfizer, devolvemos o
            # de maior confianca em vez do ultimo, que pode ser pior.
            if melhor is None or resultado.confianca > melhor.confianca:
                melhor = resultado

            if suficiente:
                if indice > 0:
                    logger.info(
                        "ocr.escalonamento_resolveu",
                        motor=resultado.motor,
                        tentativas=len(self.tentativas),
                    )
                return resultado

            if not ultimo:
                logger.info(
                    "ocr.escalando",
                    de=resultado.motor,
                    para=self.motores[indice + 1].identificacao,
                    confianca=float(resultado.confianca.valor),
                )

        if melhor is None:  # pragma: no cover - todos falharam e o ultimo relancou
            raise RuntimeError("Nenhum motor de OCR produziu resultado")

        logger.warning(
            "ocr.cadeia_esgotada",
            melhor_motor=melhor.motor,
            confianca=float(melhor.confianca.valor),
            qualidade=melhor.qualidade.value,
        )
        return melhor

    def _suficiente(self, resultado: ResultadoOCR) -> bool:
        """Decide se o resultado serve, sem precisar do proximo motor."""
        if resultado.vazio:
            return False

        # Texto rejeitado pela politica nunca e suficiente, mesmo que o
        # verificador ache os campos: abaixo de 60% a POL-002 manda reenviar.
        if resultado.qualidade is QualidadeExtracao.REJEITADA:
            return False

        if self.suficiencia is not None:
            # O verificador tem a palavra final na faixa aceitavel: ele olha o
            # que importa (os campos), nao a media da pagina.
            return self.suficiencia(resultado.texto)

        return resultado.confianca >= self.confianca_minima
