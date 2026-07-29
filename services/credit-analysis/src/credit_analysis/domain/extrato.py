"""Analise de extrato bancario com Pandas.

Um extrato e uma serie temporal de transacoes; a pergunta de credito nao e
"quanto entrou" e sim "quanto entra de forma recorrente e previsivel". Isso e
agregacao por competencia, deteccao de recorrencia e medida de dispersao —
exatamente o que Pandas faz bem e um loop Python faz mal.

O modulo recebe transacoes ja normalizadas (o parsing de OFX/CSV/PDF fica na
Camada 3, com o OCR) e devolve um resumo tipado. Sem I/O aqui.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import numpy as np
import pandas as pd

from credit_analysis.domain.exceptions import DadosInsuficientes
from credit_analysis.domain.value_objects import Dinheiro, Percentual

# Minimo de meses para que a media de renda seja estatisticamente util.
# Abaixo disso um unico mes atipico distorce tudo.
MESES_MINIMOS = 3

# Uma entrada e considerada recorrente se aparece em pelo menos esta fracao
# dos meses observados, com valor estavel.
FRACAO_MESES_RECORRENCIA = 0.6

# Coeficiente de variacao acima do qual a renda e tratada como instavel.
CV_RENDA_INSTAVEL = 0.35


@dataclass(frozen=True, slots=True)
class Transacao:
    """Uma linha do extrato, ja normalizada."""

    data: date
    descricao: str
    valor: Dinheiro  # positivo = credito, negativo = debito


@dataclass(frozen=True, slots=True)
class ResumoExtrato:
    """Indicadores derivados do extrato, prontos para alimentar o score."""

    meses_analisados: int
    renda_media_mensal: Dinheiro
    renda_mediana_mensal: Dinheiro
    despesa_media_mensal: Dinheiro
    saldo_medio_mensal: Dinheiro
    volatilidade_renda: Percentual
    renda_estavel: bool
    meses_com_saldo_negativo: int
    creditos_recorrentes: list[str]
    periodo_inicio: date
    periodo_fim: date

    @property
    def capacidade_poupanca(self) -> Dinheiro:
        """Sobra media mensal — o que efetivamente pode virar parcela."""
        return self.renda_media_mensal - self.despesa_media_mensal


def _para_dataframe(transacoes: list[Transacao]) -> pd.DataFrame:
    """Converte a lista tipada em DataFrame com colunas derivadas.

    Decimal vira float aqui de proposito: e analise estatistica, nao
    contabilidade. Os valores que voltam para o dominio sao requantizados em
    Decimal na saida.
    """
    df = pd.DataFrame(
        {
            "data": pd.to_datetime([t.data for t in transacoes]),
            "descricao": [t.descricao.strip().upper() for t in transacoes],
            "valor": [float(t.valor.valor) for t in transacoes],
        }
    )
    df["competencia"] = df["data"].dt.to_period("M")
    df["tipo"] = np.where(df["valor"] >= 0, "credito", "debito")
    return df


def _detectar_recorrentes(df: pd.DataFrame, total_meses: int) -> list[str]:
    """Descricoes de credito que se repetem em boa parte dos meses.

    Normaliza a descricao removendo digitos (numeros de documento e
    identificadores mudam a cada lancamento e quebrariam o agrupamento).
    """
    creditos = df[df["tipo"] == "credito"].copy()
    if creditos.empty:
        return []

    creditos["chave"] = (
        creditos["descricao"].str.replace(r"\d+", "", regex=True).str.strip().str.slice(0, 40)
    )

    agrupado = creditos.groupby("chave").agg(
        meses=("competencia", "nunique"),
        media=("valor", "mean"),
        desvio=("valor", "std"),
    )

    minimo_meses = max(2, int(np.ceil(total_meses * FRACAO_MESES_RECORRENCIA)))

    # std de um unico ponto e NaN; tratamos como estavel (sem variacao medida).
    cv = (agrupado["desvio"].fillna(0.0) / agrupado["media"].abs()).fillna(0.0)
    recorrentes = agrupado[(agrupado["meses"] >= minimo_meses) & (cv <= 0.25)]

    return [chave for chave in recorrentes.sort_values("media", ascending=False).index if chave]


def analisar_extrato(transacoes: list[Transacao]) -> ResumoExtrato:
    """Produz o resumo consolidado do extrato.

    Levanta DadosInsuficientes quando a janela e curta demais para sustentar
    um parecer — falhar explicitamente e melhor que devolver uma media de um
    mes so como se fosse renda comprovada.
    """
    if not transacoes:
        raise DadosInsuficientes("Extrato vazio")

    df = _para_dataframe(transacoes)
    total_meses = int(df["competencia"].nunique())

    if total_meses < MESES_MINIMOS:
        raise DadosInsuficientes(
            f"Extrato cobre {total_meses} mes(es); minimo de {MESES_MINIMOS} para analise"
        )

    # Uma linha por competencia, com entradas e saidas somadas.
    por_mes = (
        df.pivot_table(
            index="competencia",
            columns="tipo",
            values="valor",
            aggfunc="sum",
            fill_value=0.0,
        )
        .reindex(columns=["credito", "debito"], fill_value=0.0)
        .rename(columns={"credito": "entradas", "debito": "saidas"})
    )
    por_mes["saidas"] = por_mes["saidas"].abs()
    por_mes["saldo"] = por_mes["entradas"] - por_mes["saidas"]

    entradas = por_mes["entradas"]
    media_entradas = float(entradas.mean())

    # Mediana zero significa que metade ou mais dos meses nao tem credito
    # reconhecido. Isso nao e uma renda de zero — e um extrato que nao foi lido.
    # Devolver `renda_mediana = R$ 0,00` como fato faria o score tratar como
    # certeza algo que e ausencia de informacao, e o comprometimento de renda
    # (parcela / renda) daria 100% por divisao por zero em vez de "nao apurado".
    if float(entradas.median()) <= 0:
        raise DadosInsuficientes(
            f"Nenhum credito recorrente reconhecido em pelo menos metade dos "
            f"{total_meses} meses; extrato ilegivel ou sem entradas"
        )

    # Coeficiente de variacao: desvio relativo a media. Comparavel entre
    # faixas de renda diferentes, ao contrario do desvio absoluto.
    desvio = float(entradas.std(ddof=0))
    cv = desvio / media_entradas if media_entradas else 0.0

    return ResumoExtrato(
        meses_analisados=total_meses,
        renda_media_mensal=Dinheiro(Decimal(str(media_entradas))),
        renda_mediana_mensal=Dinheiro(Decimal(str(float(entradas.median())))),
        despesa_media_mensal=Dinheiro(Decimal(str(float(por_mes["saidas"].mean())))),
        saldo_medio_mensal=Dinheiro(Decimal(str(float(por_mes["saldo"].mean())))),
        volatilidade_renda=Percentual(Decimal(str(cv * 100.0))),
        renda_estavel=cv <= CV_RENDA_INSTAVEL,
        meses_com_saldo_negativo=int((por_mes["saldo"] < 0).sum()),
        creditos_recorrentes=_detectar_recorrentes(df, total_meses),
        periodo_inicio=df["data"].min().date(),
        periodo_fim=df["data"].max().date(),
    )
