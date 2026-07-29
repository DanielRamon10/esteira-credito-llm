"""Motor de score de credito.

Modelo aditivo ponderado, deliberadamente simples e explicavel: cada fator
contribui com uma pontuacao 0-1000 e a nota final e a media ponderada. Um
modelo caixa-preta daria numero melhor, mas nao daria justificativa — e a
Resolucao CMN 4.658 e a LGPD (art. 20) exigem que o cliente possa pedir
revisao de uma decisao automatizada. Explicabilidade aqui e requisito, nao
enfeite.

O calculo e puro (sem I/O, sem dependencia de framework) e usa NumPy para
manter os pesos como vetor: adicionar um fator novo e mexer em duas listas,
nao em uma cadeia de ifs.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import numpy as np

from credit_analysis.domain.entities import Parecer, PropostaCredito, Solicitante
from credit_analysis.domain.enums import Decisao, NivelRisco
from credit_analysis.domain.value_objects import Dinheiro, Percentual

# --- Limiares de politica ----------------------------------------------------
# Numeros de politica ficam nomeados e no topo do modulo, nunca inline no meio
# do calculo: e o que o time de risco revisa, e precisa ser achavel.

COMPROMETIMENTO_CONFORTAVEL = Percentual.de(30)
COMPROMETIMENTO_LIMITE = Percentual.de(50)

SCORE_APROVACAO_DIRETA = 700
SCORE_ANALISE_MANUAL = 500
SCORE_NEGATIVA = 350

IDADE_MINIMA_PREFERENCIAL = 25
IDADE_MAXIMA_PREFERENCIAL = 65


@dataclass(frozen=True, slots=True)
class FatorScore:
    """Uma dimensao avaliada, sua nota e o porque."""

    nome: str
    pontuacao: float  # 0-1000
    peso: float
    justificativa: str


@dataclass(frozen=True, slots=True)
class EntradaScore:
    """Tudo que o motor precisa. Sem isso ele nao consulta nada externo.

    `renda_comprovada` vem do OCR/extrato (Camada 3); `renda_declarada` vem do
    formulario. A divergencia entre as duas e, por si so, um fator de risco.
    """

    solicitante: Solicitante
    proposta: PropostaCredito
    renda_comprovada: Dinheiro | None = None
    meses_historico_bancario: int = 0
    tem_restricao_cadastral: bool = False
    saldo_medio: Dinheiro | None = None


def _pontuar_comprometimento(comprometimento: Percentual) -> FatorScore:
    """Quanto da renda a parcela consome. Fator de maior peso.

    Curva linear decrescente: 0% de comprometimento vale 1000, e a nota chega
    a zero em 60%. Acima disso a parcela nao cabe no orcamento em nenhuma
    hipotese razoavel.
    """
    pct = float(comprometimento.valor)
    pontos = float(np.clip(1000.0 - (pct / 60.0) * 1000.0, 0.0, 1000.0))

    if comprometimento <= COMPROMETIMENTO_CONFORTAVEL:
        just = f"Comprometimento de renda em {comprometimento} — dentro da faixa confortavel"
    elif comprometimento <= COMPROMETIMENTO_LIMITE:
        just = f"Comprometimento de renda em {comprometimento} — acima do confortavel"
    else:
        just = f"Comprometimento de renda em {comprometimento} — acima do limite de politica"

    return FatorScore("comprometimento_renda", pontos, peso=0.40, justificativa=just)


def _pontuar_divergencia_renda(declarada: Dinheiro, comprovada: Dinheiro | None) -> FatorScore:
    """Distancia entre renda declarada e renda comprovada por documento.

    Sem documento comprobatorio a nota e neutra-baixa, nao zero: a ausencia de
    comprovacao e incerteza, nao evidencia de fraude.
    """
    if comprovada is None:
        return FatorScore(
            "divergencia_renda",
            400.0,
            peso=0.20,
            justificativa="Renda nao comprovada documentalmente",
        )

    if declarada.valor == 0:
        divergencia = 100.0
    else:
        delta = abs(float(declarada.valor) - float(comprovada.valor))
        divergencia = delta / float(declarada.valor) * 100.0

    pontos = float(np.clip(1000.0 - divergencia * 20.0, 0.0, 1000.0))

    if divergencia <= 10.0:
        just = f"Renda comprovada ({comprovada}) confere com a declarada ({declarada})"
    else:
        just = (
            f"Divergencia de {divergencia:.1f}% entre renda declarada ({declarada}) "
            f"e comprovada ({comprovada})"
        )

    return FatorScore("divergencia_renda", pontos, peso=0.20, justificativa=just)


def _pontuar_historico(meses: int) -> FatorScore:
    """Profundidade do historico bancario. Satura em 24 meses."""
    pontos = float(np.clip(meses / 24.0 * 1000.0, 0.0, 1000.0))
    just = (
        f"Historico bancario de {meses} meses"
        if meses
        else "Sem historico bancario disponivel para analise"
    )
    return FatorScore("historico_bancario", pontos, peso=0.15, justificativa=just)


def _pontuar_restricao(tem_restricao: bool) -> FatorScore:
    """Restricao cadastral e binaria e domina o resultado quando presente."""
    return FatorScore(
        "restricao_cadastral",
        0.0 if tem_restricao else 1000.0,
        peso=0.15,
        justificativa=(
            "Restricao cadastral ativa" if tem_restricao else "Sem restricoes cadastrais"
        ),
    )


def _pontuar_perfil(solicitante: Solicitante) -> FatorScore:
    """Faixa etaria. Peso baixo de proposito.

    Idade correlaciona com estabilidade de renda, mas e proxy fraco e sensivel
    do ponto de vista de vies. Fica com 10% e nunca decide sozinho um caso.
    """
    idade = solicitante.idade

    if IDADE_MINIMA_PREFERENCIAL <= idade <= IDADE_MAXIMA_PREFERENCIAL:
        pontos = 1000.0
    elif idade < IDADE_MINIMA_PREFERENCIAL:
        pontos = 600.0 + (idade - 18) * 50.0
    else:
        pontos = max(400.0, 1000.0 - (idade - IDADE_MAXIMA_PREFERENCIAL) * 30.0)

    return FatorScore(
        "perfil_demografico",
        float(np.clip(pontos, 0.0, 1000.0)),
        peso=0.10,
        justificativa=f"Solicitante com {idade} anos",
    )


def calcular_fatores(entrada: EntradaScore) -> tuple[list[FatorScore], Percentual]:
    """Avalia todos os fatores e devolve tambem o comprometimento de renda."""
    renda_base = entrada.renda_comprovada or entrada.solicitante.renda_mensal_declarada
    comprometimento = entrada.proposta.parcela_mensal.razao(renda_base)

    fatores = [
        _pontuar_comprometimento(comprometimento),
        _pontuar_divergencia_renda(
            entrada.solicitante.renda_mensal_declarada, entrada.renda_comprovada
        ),
        _pontuar_historico(entrada.meses_historico_bancario),
        _pontuar_restricao(entrada.tem_restricao_cadastral),
        _pontuar_perfil(entrada.solicitante),
    ]
    return fatores, comprometimento


def consolidar_score(fatores: list[FatorScore]) -> int:
    """Media ponderada dos fatores, normalizada para 0-1000."""
    pontuacoes = np.array([f.pontuacao for f in fatores], dtype=np.float64)
    pesos = np.array([f.peso for f in fatores], dtype=np.float64)

    total_pesos = pesos.sum()
    if total_pesos == 0:
        return 0

    return round(float(np.dot(pontuacoes, pesos) / total_pesos))


def classificar_risco(score: int) -> NivelRisco:
    if score >= SCORE_APROVACAO_DIRETA:
        return NivelRisco.BAIXO
    if score >= SCORE_ANALISE_MANUAL:
        return NivelRisco.MEDIO
    if score >= SCORE_NEGATIVA:
        return NivelRisco.ALTO
    return NivelRisco.CRITICO


def decidir(score: int, comprometimento: Percentual, tem_restricao: bool) -> Decisao:
    """Regra de decisao.

    Duas condicoes vetam a aprovacao independentemente do score, porque sao
    politica dura e nao ponderavel: restricao cadastral ativa e parcela acima
    do teto de comprometimento.
    """
    if tem_restricao:
        return Decisao.NEGADO
    if comprometimento > COMPROMETIMENTO_LIMITE:
        return Decisao.NEGADO
    if score >= SCORE_APROVACAO_DIRETA:
        if comprometimento <= COMPROMETIMENTO_CONFORTAVEL:
            return Decisao.APROVADO
        return Decisao.APROVADO_COM_RESSALVAS
    if score >= SCORE_ANALISE_MANUAL:
        return Decisao.ANALISE_MANUAL
    return Decisao.NEGADO


def calcular_limite_recomendado(entrada: EntradaScore, score: int) -> Dinheiro:
    """Maior valor cujo comprometimento fica na faixa confortavel.

    Inverte a Tabela Price: dado o teto de parcela, qual principal cabe.
    O score modula o teto — quem pontua alto acessa a faixa cheia.
    """
    renda = entrada.renda_comprovada or entrada.solicitante.renda_mensal_declarada
    fator_score = Decimal(str(min(1.0, max(0.3, score / 1000.0))))
    parcela_maxima = renda * (COMPROMETIMENTO_CONFORTAVEL.fracao * fator_score)

    i = entrada.proposta.taxa_juros_mensal.fracao
    n = entrada.proposta.prazo_meses

    if i == 0:
        return parcela_maxima * n

    fator = (Decimal(1) + i) ** n
    principal = parcela_maxima.valor * (fator - Decimal(1)) / (i * fator)
    return Dinheiro(principal)


def avaliar(entrada: EntradaScore) -> Parecer:
    """Ponto de entrada do motor: da entrada ao parecer completo."""
    fatores, comprometimento = calcular_fatores(entrada)
    score = consolidar_score(fatores)
    decisao = decidir(score, comprometimento, entrada.tem_restricao_cadastral)

    return Parecer(
        decisao=decisao,
        nivel_risco=classificar_risco(score),
        score=score,
        comprometimento_renda=comprometimento,
        justificativas=[f.justificativa for f in fatores],
        limite_recomendado=calcular_limite_recomendado(entrada, score),
    )
