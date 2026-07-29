"""Value objects do dominio.

Sao imutaveis, validam a propria invariante na construcao e nao tem
identidade: dois CPFs com o mesmo numero sao o mesmo valor. Nenhum
depende de framework — dominio puro, testavel sem infraestrutura.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Self

from credit_analysis.domain.exceptions import MoedasIncompativeis, ValorInvalido

_CENTAVOS = Decimal("0.01")
_SOMENTE_DIGITOS = re.compile(r"\D")


@dataclass(frozen=True, slots=True)
class CPF:
    """CPF validado pelos dois digitos verificadores.

    Guarda apenas os 11 digitos; a formatacao com pontuacao e responsabilidade
    da camada de apresentacao.
    """

    numero: str

    def __post_init__(self) -> None:
        digitos = _SOMENTE_DIGITOS.sub("", self.numero)

        if len(digitos) != 11:
            raise ValorInvalido(f"CPF deve ter 11 digitos, recebeu {len(digitos)}")

        # Sequencias repetidas (000..., 111...) passam no calculo do DV mas nao
        # sao CPFs reais — a Receita as reserva e nunca as emite.
        if digitos == digitos[0] * 11:
            raise ValorInvalido("CPF com todos os digitos iguais e invalido")

        if digitos[9:] != _digitos_verificadores(digitos[:9]):
            raise ValorInvalido("Digitos verificadores do CPF nao conferem")

        object.__setattr__(self, "numero", digitos)

    @property
    def mascarado(self) -> str:
        """Formato ***.456.789-** para logs e respostas de API.

        Nunca exponha o CPF completo em log: e dado pessoal sob a LGPD.
        """
        return f"***.{self.numero[3:6]}.{self.numero[6:9]}-**"

    def __str__(self) -> str:
        return f"{self.numero[:3]}.{self.numero[3:6]}.{self.numero[6:9]}-{self.numero[9:]}"


def _digitos_verificadores(base: str) -> str:
    """Calcula os dois DVs a partir dos 9 primeiros digitos."""
    digitos = [int(c) for c in base]

    for _ in range(2):
        peso = len(digitos) + 1
        soma = sum(d * (peso - i) for i, d in enumerate(digitos))
        resto = soma % 11
        digitos.append(0 if resto < 2 else 11 - resto)

    return "".join(str(d) for d in digitos[9:])


@dataclass(frozen=True, slots=True, order=True)
class Dinheiro:
    """Valor monetario em Decimal, arredondado a centavos.

    Usar float para dinheiro acumula erro de representacao binaria; em uma
    esteira de credito isso vira divergencia de centavos entre o parecer e o
    contrato. Decimal com quantize explicito elimina a classe inteira de bug.
    """

    valor: Decimal
    moeda: str = "BRL"

    def __post_init__(self) -> None:
        try:
            quantizado = Decimal(self.valor).quantize(_CENTAVOS, rounding=ROUND_HALF_UP)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValorInvalido(f"Valor monetario invalido: {self.valor!r}") from exc

        if not quantizado.is_finite():
            raise ValorInvalido("Valor monetario deve ser finito")

        object.__setattr__(self, "valor", quantizado)
        object.__setattr__(self, "moeda", self.moeda.upper())

    @classmethod
    def zero(cls, moeda: str = "BRL") -> Self:
        return cls(Decimal("0"), moeda)

    @classmethod
    def de(cls, valor: Decimal | int | str, moeda: str = "BRL") -> Self:
        """Construtor tolerante a int/str. Nao aceita float de proposito."""
        return cls(Decimal(str(valor)), moeda)

    def _exigir_mesma_moeda(self, outro: Dinheiro) -> None:
        if self.moeda != outro.moeda:
            raise MoedasIncompativeis(f"Nao e possivel operar {self.moeda} com {outro.moeda}")

    def __add__(self, outro: Dinheiro) -> Dinheiro:
        self._exigir_mesma_moeda(outro)
        return Dinheiro(self.valor + outro.valor, self.moeda)

    def __sub__(self, outro: Dinheiro) -> Dinheiro:
        self._exigir_mesma_moeda(outro)
        return Dinheiro(self.valor - outro.valor, self.moeda)

    def __mul__(self, fator: Decimal | int) -> Dinheiro:
        return Dinheiro(self.valor * Decimal(str(fator)), self.moeda)

    def razao(self, outro: Dinheiro) -> Percentual:
        """Proporcao entre dois valores, como percentual.

        Divisao por zero e um caso de negocio real (renda declarada zerada),
        entao devolve 100% em vez de estourar: comprometimento total.
        """
        self._exigir_mesma_moeda(outro)
        if outro.valor == 0:
            return Percentual(Decimal("100"))
        return Percentual(self.valor / outro.valor * Decimal("100"))

    @property
    def positivo(self) -> bool:
        return self.valor > 0

    def __str__(self) -> str:
        inteiro, _, centavos = f"{self.valor:.2f}".partition(".")
        milhar = f"{int(inteiro):,}".replace(",", ".")
        return f"R$ {milhar},{centavos}"


@dataclass(frozen=True, slots=True, order=True)
class Percentual:
    """Percentual na escala 0-100 (nao 0-1), limitado a duas casas."""

    valor: Decimal

    def __post_init__(self) -> None:
        try:
            quantizado = Decimal(self.valor).quantize(_CENTAVOS, rounding=ROUND_HALF_UP)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValorInvalido(f"Percentual invalido: {self.valor!r}") from exc

        if quantizado < 0:
            raise ValorInvalido("Percentual nao pode ser negativo")

        object.__setattr__(self, "valor", quantizado)

    @classmethod
    def de(cls, valor: Decimal | int | float | str) -> Self:
        return cls(Decimal(str(valor)))

    @property
    def fracao(self) -> Decimal:
        """Escala 0-1, para multiplicar por valores monetarios."""
        return self.valor / Decimal("100")

    def __str__(self) -> str:
        return f"{self.valor:.2f}%".replace(".", ",")
