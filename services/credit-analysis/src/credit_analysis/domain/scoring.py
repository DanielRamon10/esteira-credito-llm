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

from dataclasses import dataclass, replace
from decimal import Decimal

import numpy as np

from credit_analysis.domain.entities import Parecer, PropostaCredito, Solicitante
from credit_analysis.domain.enums import Decisao, NivelRisco
from credit_analysis.domain.kyc import DecisaoKYC, ResultadoKYC
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


def aplicar_gate_kyc(parecer: Parecer, kyc: ResultadoKYC) -> Parecer:
    """Compoe o parecer de credito com a triagem de conformidade.

    ## Por que um gate separado, e nao um fator do score

    Seria tentador somar KYC como sexto fator com um peso. Estaria errado: score e
    uma medida **graduada** de risco de credito, e conformidade e **binaria** por
    natureza. Uma pessoa em lista de sancoes nao tem "risco alto" — ela nao pode
    operar, e nenhuma combinacao de renda alta e historico impecavel compensa isso.
    Virar peso significaria que um score excelente poderia diluir uma sancao.

    E o mesmo raciocinio que faz restricao cadastral ser veto e nao peso.

    ## O gate so aperta, nunca afrouxa

    Nenhum caminho aqui melhora uma decisao — e garantir isso exige mais cuidado do
    que parece. A primeira versao fazia `replace(parecer, decisao=ANALISE_MANUAL)`
    direto, e um teste de propriedade derrubou na hora: um parecer **NEGADO** pelo
    motor de score era promovido a analise manual porque a pessoa era PEP. Ou seja,
    a exigencia regulatoria de diligencia reforcada estava **abrindo** um caso que o
    score havia negado.

    Por isso a composicao usa `_decisao_mais_restritiva`: o gate declara o **piso**
    de restricao que a conformidade exige, e a decisao final e a mais severa entre
    esse piso e a do score. Assim a propriedade "o gate so aperta" deixa de depender
    de disciplina de quem edita e passa a ser estrutural.

    A ordem das clausulas e a de severidade, e cada uma acrescenta a justificativa
    em vez de substitui-la — o parecer final carrega o porque do score **e** o porque
    do gate.
    """
    if kyc.decisao is DecisaoKYC.APROVADO:
        return parecer

    justificativas = [*parecer.justificativas, *kyc.justificativas]

    if kyc.veta:
        # Veto duro, no mesmo nivel da restricao cadastral: score irrelevante.
        return replace(
            parecer,
            decisao=_decisao_mais_restritiva(parecer.decisao, Decisao.NEGADO),
            nivel_risco=_risco_mais_alto(parecer.nivel_risco, NivelRisco.CRITICO),
            justificativas=[
                *justificativas,
                "Reprovado na triagem de conformidade: veto independente do score",
            ],
        )

    if kyc.indisponivel:
        # Nao aprovar sem verificar (violacao regulatoria) e nao negar por causa de
        # uma indisponibilidade nossa (injusto). Vai para humano, com o motivo dito —
        # a menos que o score ja tenha negado, e nesse caso a negativa fica.
        motivo = (
            "Analise manual obrigatoria: nao foi possivel concluir a triagem de "
            "conformidade. Aprovar sem ela seria descumprir a diligencia exigida; "
            "negar puniria o cliente por indisponibilidade interna."
        )
    elif kyc.decisao is DecisaoKYC.APROVADO_COM_DILIGENCIA:
        motivo = (
            "Pessoa Exposta Politicamente: aprovacao exige alcada superior "
            "(Circular BCB 3.978 art. 27). Nao e impedimento."
        )
    else:
        motivo = "Triagem de conformidade encaminhada a revisao humana"

    return replace(
        parecer,
        decisao=_decisao_mais_restritiva(parecer.decisao, Decisao.ANALISE_MANUAL),
        justificativas=[*justificativas, motivo],
    )


# Ordem de severidade das decisoes, do mais permissivo ao mais restritivo.
#
# Explicita numa tupla, e nao inferida da ordem de declaracao do enum: a ordem do
# enum e uma escolha de leitura e pode ser reorganizada sem que ninguem perceba que
# quebrou a comparacao. Aqui a intencao esta escrita.
_SEVERIDADE_DECISAO = (
    Decisao.APROVADO,
    Decisao.APROVADO_COM_RESSALVAS,
    Decisao.ANALISE_MANUAL,
    Decisao.NEGADO,
)

_SEVERIDADE_RISCO = (
    NivelRisco.BAIXO,
    NivelRisco.MEDIO,
    NivelRisco.ALTO,
    NivelRisco.CRITICO,
)


def _decisao_mais_restritiva(a: Decisao, b: Decisao) -> Decisao:
    """A mais severa entre duas decisoes.

    E o que torna "o gate so aperta" uma propriedade do codigo em vez de uma
    promessa do comentario.
    """
    return max(a, b, key=_SEVERIDADE_DECISAO.index)


def _risco_mais_alto(a: NivelRisco, b: NivelRisco) -> NivelRisco:
    return max(a, b, key=_SEVERIDADE_RISCO.index)
