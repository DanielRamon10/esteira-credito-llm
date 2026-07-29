"""Triagem de KYC: classificacao, decisao e trilha.

## A decisao e deterministica, e isso e o ponto

Mesma disciplina do motor de score de credito do outro servico: quem decide e
codigo com regra explicita, e cada decisao carrega a frase que a justifica. A
Circular BCB 3.978 e o art. 20 da LGPD nao aceitam "o sistema apontou" como
fundamentacao — e um analista que recebe um score sem explicacao aprende a
ignora-lo.

Nao ha LLM neste servico. Nao e economia: casamento de nome e problema lexical, e
uma decisao de conformidade que precisa ser identica hoje e em seis meses nao
combina com modelo generativo. O contraste com o `credit-analysis` e proposital —
lá o LLM redige e o motor decide; aqui nao ha o que redigir.

## Limiares, medidos e nao arbitrados

Sobre 16 pares de nome (8 da mesma pessoa escritos de formas diferentes, 8 de
pessoas distintas), o algoritmo de `matching.py` produziu:

    mesma pessoa      score minimo   0,934
    pessoas distintas score maximo   0,703

Ha uma faixa vazia de 0,23 entre os dois grupos. `LIMIAR_FORTE = 0,85` fica no
meio dela, com margem para os dois lados — e nao encostado num dos extremos, que
e o que torna um limiar fragil a primeira variacao.

`LIMIAR_PARCIAL = 0,62` e outra decisao, com outra logica: ele **nao** separa
certo de errado, e sim define o que vale o tempo de um analista. Fica abaixo do
maior negativo de proposito, porque nesta faixa moram os casos genuinamente
ambiguos — nome consultado que e subconjunto do da lista ("Jose da Silva" contra
"Jose da Silva Rodrigues", 0,703) ou nome reordenado. Falso positivo aqui custa
revisao; falso negativo custa violacao regulatoria.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from kyc_compliance.domain.matching import (
    Correspondencia,
    NivelCorrespondencia,
    comparar,
    normalizar,
)

# Igualdade apos normalizacao, ou CPF identico.
LIMIAR_EXATO = 0.97

# No meio da faixa vazia entre 0,703 e 0,934 (ver o cabecalho).
LIMIAR_FORTE = 0.85

# Piso do que vale revisao humana. Abaixo disso o caso segue sem intervencao.
LIMIAR_PARCIAL = 0.62


class TipoLista(StrEnum):
    """Origem da restricao. O tipo muda a consequencia, nao apenas o rotulo."""

    # Pessoa Exposta Politicamente. **Nao e impedimento** — e a fonte de confusao
    # mais comum nesta area. PEP exige diligencia reforcada e aprovacao por alcada
    # superior (Circular BCB 3.978, art. 27), nao recusa. Tratar PEP como bloqueio
    # nega credito a milhares de servidores publicos sem base legal.
    PEP = "pep"

    # Sancao nacional ou internacional. Aqui sim ha impedimento: operar com pessoa
    # sancionada expoe a instituicao a penalidade propria.
    SANCAO = "sancao"

    # Midia negativa adversa (investigacao noticiada, condenacao). Sinal, nao veto:
    # exige avaliacao caso a caso.
    MIDIA_NEGATIVA = "midia_negativa"


class NivelRiscoKYC(StrEnum):
    BAIXO = "baixo"
    MEDIO = "medio"
    ALTO = "alto"
    INACEITAVEL = "inaceitavel"


class DecisaoKYC(StrEnum):
    APROVADO = "aprovado"
    # Aprovado, porem sob diligencia reforcada — o caso do PEP.
    APROVADO_COM_DILIGENCIA = "aprovado_com_diligencia"
    REVISAO_MANUAL = "revisao_manual"
    REPROVADO = "reprovado"


@dataclass(frozen=True, slots=True)
class EntradaRestritiva:
    """Uma pessoa numa lista de restricao.

    `cpf` e opcional porque lista publica frequentemente nao o traz — a de
    sancoes da ONU, por exemplo, tem nome e data de nascimento. Quando existe, ele
    domina a decisao: CPF identico dispensa qualquer discussao sobre nome.
    """

    nome: str
    tipo: TipoLista
    origem: str
    cpf: str | None = None
    cargo: str | None = None
    observacao: str | None = None


@dataclass(frozen=True, slots=True)
class Triagem:
    """Resultado de uma consulta, com tudo que a auditoria precisa reconstruir."""

    nome_consultado: str
    cpf_consultado: str
    decisao: DecisaoKYC
    nivel_risco: NivelRiscoKYC
    correspondencias: tuple[Correspondencia, ...] = field(default=())
    justificativas: tuple[str, ...] = field(default=())
    entradas_avaliadas: int = 0
    id: UUID = field(default_factory=uuid4)
    criada_em: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def aprovado(self) -> bool:
        return self.decisao in {DecisaoKYC.APROVADO, DecisaoKYC.APROVADO_COM_DILIGENCIA}

    @property
    def cpf_mascarado(self) -> str:
        """Mesma politica do outro servico: CPF completo nao sai em resposta."""
        digitos = "".join(c for c in self.cpf_consultado if c.isdigit())
        if len(digitos) != 11:
            return "***"
        return f"***.{digitos[3:6]}.{digitos[6:9]}-**"


def classificar(score: float, cpf_confere: bool) -> NivelCorrespondencia:
    """Traduz score em nivel nomeado.

    ## CPF identico nao basta para veto automatico

    A primeira versao devolvia `EXATA` sempre que o CPF conferia, com o argumento
    de que documento e identificador mais forte que nome. Exercitando o servico
    contra a lista de verdade, o resultado apareceu: um cliente com CPF igual ao de
    um sancionado, mas nome **sem nenhuma palavra em comum**, era reprovado
    automaticamente, risco `inaceitavel`, sem passar por humano.

    O raciocinio estava incompleto. CPF igual com nome compativel e identificacao
    dupla — evidencia mais forte que existe. CPF igual com nome completamente
    diferente e outra coisa: a explicacao mais provavel nao e mudanca de nome, e
    **erro de digitacao no cadastro** — na lista ou na consulta. E um digito errado
    num arquivo publico nao pode negar credito a uma pessoa sem revisao.

    Entao: CPF + nome compativel devolve `EXATA` (veto vale); CPF isolado devolve
    `PARCIAL`, que na regra de decisao leva a revisao manual. A flag `cpf_confere`
    continua na correspondencia e na justificativa, entao o analista ve exatamente
    o que aconteceu — nenhum sinal se perde, so a automacao do veto.
    """
    if cpf_confere:
        return (
            NivelCorrespondencia.EXATA if score >= LIMIAR_PARCIAL else NivelCorrespondencia.PARCIAL
        )
    if score >= LIMIAR_EXATO:
        return NivelCorrespondencia.EXATA
    if score >= LIMIAR_FORTE:
        return NivelCorrespondencia.FORTE
    if score >= LIMIAR_PARCIAL:
        return NivelCorrespondencia.PARCIAL
    return NivelCorrespondencia.NENHUMA


def avaliar(
    nome: str,
    cpf: str,
    entradas: list[EntradaRestritiva],
) -> Triagem:
    """Compara contra todas as entradas e decide.

    Percorre a lista inteira em vez de parar no primeiro achado: a pessoa pode
    constar como PEP **e** em sancao, e as duas consequencias sao diferentes.
    Parar no primeiro daria uma decisao que depende da ordem do arquivo.
    """
    # Guarda o par (correspondencia, entrada) em vez de indexar a entrada por nome
    # depois. Um dicionario por nome colapsaria a pessoa que consta em DUAS listas
    # com o mesmo nome — que e precisamente o caso PEP + sancao, o mais importante
    # de nao perder.
    achados: list[tuple[Correspondencia, EntradaRestritiva]] = []
    digitos_consultados = _digitos(cpf)

    for entrada in entradas:
        cpf_confere = bool(
            digitos_consultados and entrada.cpf and _digitos(entrada.cpf) == digitos_consultados
        )
        score, casados, ausentes = comparar(nome, entrada.nome)

        # CPF identico com nome diferente nao e erro: e mudanca de nome, ou erro de
        # cadastro na lista. O score do nome fica registrado, mas o nivel sobe.
        nivel = classificar(score, cpf_confere)
        if nivel is NivelCorrespondencia.NENHUMA:
            continue

        achados.append(
            (
                Correspondencia(
                    nome_consultado=normalizar(nome),
                    nome_na_lista=entrada.nome,
                    score=score,
                    nivel=nivel,
                    tokens_casados=casados,
                    tokens_ausentes=ausentes,
                    cpf_confere=cpf_confere,
                ),
                entrada,
            )
        )

    # Mais forte primeiro: e o que o analista precisa ver no topo.
    achados.sort(key=lambda par: (par[0].cpf_confere, par[0].score), reverse=True)
    correspondencias = [c for c, _ in achados]

    decisao, risco, justificativas = _decidir(achados)

    return Triagem(
        nome_consultado=nome,
        cpf_consultado=cpf,
        decisao=decisao,
        nivel_risco=risco,
        correspondencias=tuple(correspondencias),
        justificativas=tuple(justificativas),
        entradas_avaliadas=len(entradas),
    )


def _decidir(
    achados: list[tuple[Correspondencia, EntradaRestritiva]],
) -> tuple[DecisaoKYC, NivelRiscoKYC, list[str]]:
    """Regra de decisao, explicita e por tipo de lista.

    A ordem das clausulas e a ordem de severidade, e cada uma produz sua propria
    justificativa. Um `if` encadeado sem frase associada seria mais curto e
    impossivel de explicar a quem recebe a recusa.
    """
    if not achados:
        return (
            DecisaoKYC.APROVADO,
            NivelRiscoKYC.BAIXO,
            ["Nenhuma correspondencia em lista restritiva"],
        )

    def tem(tipo: TipoLista, *niveis: NivelCorrespondencia) -> bool:
        return any(e.tipo == tipo and c.nivel in niveis for c, e in achados)

    fortes = (NivelCorrespondencia.EXATA, NivelCorrespondencia.FORTE)

    # Tres justificativas no maximo: a lista serve para o analista decidir, e vinte
    # linhas de homonimo parcial afogam a que importa. As correspondencias completas
    # ficam no campo proprio da triagem.
    justificativas = [c.justificativa for c, _ in achados[:3]]

    # Sancao com casamento forte e o unico veto duro. Operar com pessoa sancionada
    # expoe a instituicao a penalidade propria, e nao ha alcada que autorize.
    if tem(TipoLista.SANCAO, *fortes):
        return (
            DecisaoKYC.REPROVADO,
            NivelRiscoKYC.INACEITAVEL,
            [*justificativas, "Correspondencia forte em lista de sancoes: veto de conformidade"],
        )

    # Sancao com casamento parcial vai para humano — nao se reprova alguem por
    # semelhanca de nome, e nao se aprova ignorando o sinal.
    if tem(TipoLista.SANCAO, NivelCorrespondencia.PARCIAL):
        return (
            DecisaoKYC.REVISAO_MANUAL,
            NivelRiscoKYC.ALTO,
            [
                *justificativas,
                "Possivel homonimo em lista de sancoes: exige verificacao documental",
            ],
        )

    # PEP nao impede: exige diligencia reforcada e alcada superior.
    if tem(TipoLista.PEP, *fortes):
        return (
            DecisaoKYC.APROVADO_COM_DILIGENCIA,
            NivelRiscoKYC.MEDIO,
            [
                *justificativas,
                "Pessoa Exposta Politicamente: diligencia reforcada e aprovacao por "
                "alcada superior (Circular BCB 3.978 art. 27). Nao e impedimento.",
            ],
        )

    if tem(TipoLista.MIDIA_NEGATIVA, *fortes):
        return (
            DecisaoKYC.REVISAO_MANUAL,
            NivelRiscoKYC.ALTO,
            [*justificativas, "Midia negativa adversa: avaliacao caso a caso"],
        )

    # Restou apenas casamento parcial em lista nao impeditiva.
    return (
        DecisaoKYC.REVISAO_MANUAL,
        NivelRiscoKYC.MEDIO,
        [*justificativas, "Correspondencia parcial: confirmar identidade antes de prosseguir"],
    )


def _digitos(valor: str) -> str:
    return "".join(c for c in valor if c.isdigit())
