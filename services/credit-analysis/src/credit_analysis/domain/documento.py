"""Conceitos de dominio da extracao documental.

Os limiares de confianca vem da POL-002 secao 3.2 e ficam aqui, no dominio,
porque sao **regra de negocio** e nao detalhe de OCR: quem decide que 85% exige
conferencia humana e o time de risco, nao a biblioteca de reconhecimento.
Deixa-los no adapter faria a politica mudar junto com a troca de motor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum

import numpy as np
import numpy.typing as npt

from credit_analysis.domain.value_objects import Dinheiro, Percentual

# Matriz de pixels em tons de cinza (2-D) ou BGR (3-D).
ImagemDocumento = npt.NDArray[np.uint8]

# POL-002 secao 3.2: abaixo de 85% o documento vai para conferencia humana
# antes de compor o parecer; abaixo de 60% e rejeitado e o cliente reenvia.
CONFIANCA_MINIMA_AUTOMATICA = Percentual.de(85)
CONFIANCA_MINIMA_ACEITAVEL = Percentual.de(60)


class QualidadeExtracao(StrEnum):
    """Veredito sobre o texto extraido, na linguagem da politica."""

    CONFIAVEL = "confiavel"  # segue automatico
    REVISAO_HUMANA = "revisao_humana"  # 60% a 85%
    REJEITADA = "rejeitada"  # abaixo de 60%


class OrigemDaRenda(StrEnum):
    """De qual campo do holerite a renda apurada saiu.

    Existe porque os dois nao valem o mesmo: o liquido e o que entra na conta e paga parcela; o
    bruto e ~20% maior e superestima a capacidade de pagamento. Sem registrar a origem, o parecer
    apresenta os dois como "renda comprovada" e ninguem depois sabe qual foi.

    `BASE` nao e um erro — e um caso que exige um analista. Ver
    `ExtracaoHolerite.origem_da_renda`.
    """

    LIQUIDO = "liquido"
    BASE = "base"


def classificar_qualidade(confianca: Percentual) -> QualidadeExtracao:
    if confianca >= CONFIANCA_MINIMA_AUTOMATICA:
        return QualidadeExtracao.CONFIAVEL
    if confianca >= CONFIANCA_MINIMA_ACEITAVEL:
        return QualidadeExtracao.REVISAO_HUMANA
    return QualidadeExtracao.REJEITADA


@dataclass(frozen=True, slots=True)
class ResultadoOCR:
    """Texto extraido de uma imagem, com procedencia e confianca."""

    texto: str
    confianca: Percentual
    motor: str
    palavras_reconhecidas: int = 0
    correcoes_aplicadas: tuple[str, ...] = ()

    @property
    def qualidade(self) -> QualidadeExtracao:
        return classificar_qualidade(self.confianca)

    @property
    def aproveitavel(self) -> bool:
        """Se o texto pode alimentar a esteira, ainda que sob revisao."""
        return self.qualidade is not QualidadeExtracao.REJEITADA

    @property
    def vazio(self) -> bool:
        return not self.texto.strip()


# --- Parsing de valores em formato brasileiro --------------------------------

# Formato pt-BR: ponto como separador de milhar, virgula como decimal. Ler
# "8.500,00" com `Decimal(texto)` daria 8.5 — erro de tres ordens de grandeza
# num campo de renda, e sem excecao para avisar.
_MILHAR = "."
_DECIMAL = ","


def parsear_valor_brl(texto: str) -> Decimal | None:
    """Converte "8.500,00" ou "R$ 1.234,56" em Decimal. None se nao parecer valor.

    Devolve None em vez de levantar porque a entrada vem de OCR: campo ilegivel
    e caso esperado, nao excecao.
    """
    limpo = (
        texto.replace("R$", "")
        .replace("RS", "")  # OCR confunde $ com S
        .replace(" ", "")
        .replace(" ", "")
        .strip()
    )
    if not limpo:
        return None

    negativo = limpo.startswith("-") or limpo.endswith("-")
    limpo = limpo.strip("-")

    # Sufixo C/D de extrato bancario (credito/debito).
    if limpo.endswith(("C", "D")):
        negativo = negativo or limpo.endswith("D")
        limpo = limpo[:-1].strip()

    if not limpo or not any(c.isdigit() for c in limpo):
        return None

    if _DECIMAL in limpo:
        limpo = limpo.replace(_MILHAR, "").replace(_DECIMAL, ".")
    else:
        # Sem virgula: pontos sao separador de milhar ("8.500" = 8500), a menos
        # que o ultimo grupo tenha 1 ou 2 digitos ("1.5" provavelmente e
        # decimal com a virgula lida como ponto pelo OCR).
        partes = limpo.split(_MILHAR)
        if len(partes) > 1 and len(partes[-1]) in (1, 2):
            limpo = "".join(partes[:-1]) + "." + partes[-1]
        else:
            limpo = limpo.replace(_MILHAR, "")

    try:
        valor = Decimal(limpo)
    except InvalidOperation:
        return None

    return -valor if negativo else valor


def parsear_dinheiro_brl(texto: str) -> Dinheiro | None:
    valor = parsear_valor_brl(texto)
    return None if valor is None else Dinheiro(valor)


@dataclass(frozen=True, slots=True)
class CampoExtraido:
    """Um campo localizado no texto, com o rastro de como foi obtido.

    `trecho_origem` guarda a linha em que o campo foi encontrado. E o que
    permite ao analista conferir a extracao sem reabrir o documento — e o que
    torna a extracao auditavel em vez de magica.
    """

    nome: str
    valor_bruto: str
    trecho_origem: str
    confianca: Percentual

    @property
    def valor_monetario(self) -> Dinheiro | None:
        return parsear_dinheiro_brl(self.valor_bruto)


@dataclass(frozen=True, slots=True)
class ExtracaoHolerite:
    """Campos de interesse de um holerite."""

    nome: CampoExtraido | None = None
    cpf: CampoExtraido | None = None
    empregador: CampoExtraido | None = None
    competencia: CampoExtraido | None = None
    salario_base: CampoExtraido | None = None
    salario_liquido: CampoExtraido | None = None
    campos_nao_reconhecidos: tuple[str, ...] = field(default=())

    @property
    def renda_comprovada(self) -> Dinheiro | None:
        """Renda a usar no score.

        O liquido tem precedencia sobre o base: e o que efetivamente entra na
        conta do cliente e, portanto, o que pode pagar parcela. Usar o bruto
        infla a capacidade de pagamento em ~20%.

        **A queda para o bruto nao e mais silenciosa.** Ela continua acontecendo — recusar o
        documento inteiro por falta de um rotulo custaria disponibilidade — mas `origem_da_renda`
        registra qual fonte respondeu, e um caso apurado pelo bruto vai para revisao humana. Ver
        `origem_da_renda`.
        """
        for campo in (self.salario_liquido, self.salario_base):
            if campo is not None:
                valor = campo.valor_monetario
                if valor is not None and valor.positivo:
                    return valor
        return None

    @property
    def origem_da_renda(self) -> OrigemDaRenda | None:
        """De qual campo a renda apurada veio, ou None quando nao houve renda.

        ## Por que isto existe

        A medicao que motivou este campo esta em `tests/eval/test_ocr_qualidade.py`: sob o perfil
        `pouca_luz` o Tesseract saia com 88,97% de confianca — acima do limiar de 85% da POL-002 — e
        a renda apurada virava R$ 8.500,00 em vez de R$ 7.262,14. 17% acima, na direcao que aprova
        credito que nao deveria, e o parecer nao tinha como dizer que aquele numero era o bruto.

        **Aquele caso especifico foi corrigido na origem** e nao chega mais aqui: o OCR escrevia
        `7.262 , 14`, com espaco em volta da virgula, e o padrao de valor nao tolerava. Ver `_VALOR`
        em `infrastructure/ocr/extracao.py`. Ler o numero certo e melhor que sinalizar o errado.

        O campo continua, e nao por inercia: a queda para o bruto e alcancavel sempre que o liquido
        for ilegivel de verdade — rotulo apagado, coluna cortada na digitalizacao, holerite que so
        imprime o bruto. O que a correcao removeu foi um gatilho por espaco; o caso legitimo fica.

        ## Propriedade derivada, e nao campo gravado

        Um campo `origem` preenchido por quem constroi a extracao pode divergir do valor que
        `renda_comprovada` devolveu — bastaria uma ordem de precedencia mudar num lugar e nao no
        outro. Derivando da mesma condicao, as duas nao tem como discordar.
        """
        for campo, origem in (
            (self.salario_liquido, OrigemDaRenda.LIQUIDO),
            (self.salario_base, OrigemDaRenda.BASE),
        ):
            if campo is not None:
                valor = campo.valor_monetario
                if valor is not None and valor.positivo:
                    return origem
        return None

    @property
    def completa(self) -> bool:
        """Se ha o minimo para compor um parecer: identificacao e renda.

        **Nao exige que a renda venha do liquido**, e a escolha e deliberada: um holerite em que so
        o bruto ficou legivel ainda sustenta um parecer, desde que um analista olhe. Exigir o
        liquido aqui trocaria um numero 17% otimista por uma esteira que recusa documento legivel.

        Quem cuida do "desde que um analista olhe" e
        `ResultadoProcessamento.exige_revisao_humana`; quem tenta evitar o caso antes disso e
        `holerite_suficiente`, que **exige** o liquido para nao escalar cedo demais.
        """
        return self.renda_comprovada is not None and (self.cpf is not None or self.nome is not None)
