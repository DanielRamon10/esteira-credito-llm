"""Fronteira de divulgacao: o que pode ser dito a um cliente.

## O problema que os outros dois servicos nao tem

`credit-analysis` e `kyc-compliance` respondem a **analistas**. Este responde ao
**cliente**, e isso muda o que pode sair na resposta.

Revelar o limiar interno de score nao e vazamento de dado pessoal — e pior de outra
forma: e entregar o mapa para burlar a esteira. "Score minimo 700" diz a quem foi
negado exatamente quanto falta simular; "comprometimento acima de 50% e vedado" diz
qual valor pedir para passar; "alcada de gerencia aprova ate R$ 150 mil" diz a quem
insistir e com quem.

Nada disso e segredo comercial glamouroso. E simplesmente informacao que muda o
comportamento de quem a recebe, e num sistema de credito isso tem custo direto.

## Duas defesas, em lugares diferentes

**Na entrada:** a busca filtra artigos marcados como internos. E a defesa principal,
porque o modelo nao pode revelar o que nunca viu.

**Na saida:** este modulo. Existe porque a primeira defesa tem duas brechas reais —
o modelo pode saber do proprio treinamento ("bancos costumam exigir score acima
de 700"), e um artigo publico pode citar um numero interno por engano de quem o
escreveu. Uma defesa unica na entrada assume que o corpus esta perfeito e que o
modelo nao inventa; nenhuma das duas se sustenta.

## Por que substituir, e nao redigir por cima

A resposta obvia seria mascarar o trecho vazado. Esta errado: `"o limiar e
[removido]"` **confirma que existe um limiar** e informa que ele foi considerado
relevante o suficiente para ser censurado. Redacao parcial vaza a existencia do
dado, que muitas vezes e a parte util.

Quando ha vazamento, a prosa do modelo e **descartada inteira** e a resposta passa a
ser o texto do artigo publico, que ja foi revisado por gente. Perde-se fluencia;
ganha-se uma garantia que nao depende do modelo ter se comportado.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum


class Visibilidade(StrEnum):
    """Quem pode ver um artigo da base.

    Enum e nao booleano porque a lista tende a crescer (`parceiro`, `interno_risco`),
    e um `bool publico` obrigaria a reescrever todos os pontos de uso no dia em que
    aparecer o terceiro nivel.
    """

    PUBLICA = "publica"
    INTERNA = "interna"


# Padroes de conteudo que nao sai para cliente.
#
# Cada um tem nome, e o nome vai para o log e para a metrica: "vazamento detectado"
# sem dizer de que tipo nao permite corrigir o prompt nem o corpus.
_PADROES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Referencia a documento interno. O cliente nao deve nem saber a nomenclatura.
    ("referencia_politica_interna", re.compile(r"\bpol-\d{3}\b")),
    # Numero de score em qualquer posicao.
    #
    # A primeira versao tentava ser precisa: exigia "minimo", "acima de", "superior
    # a" perto do numero. Duas frases obvias passaram na medicao — "o score ficou
    # **abaixo** de 700 pontos" (comparador que eu nao previ) e "Score minimo para
    # aprovacao automatica: 700" (numero a mais de 10 caracteres da palavra).
    #
    # A licao e que enumerar as formas de dizer a mesma coisa e uma corrida perdida:
    # sempre falta uma, e a que falta e a que vaza. O padrao passou a ser
    # **proximidade** — "score" e um numero de tres digitos na mesma vizinhanca, em
    # qualquer ordem.
    #
    # `\d{3}` e nao `\d{3,4}`: os limiares deste sistema sao 350, 500 e 700, e aceitar
    # quatro digitos pegaria ano ("desde 2024") sem necessidade. Falso positivo aqui
    # nao e barato — ele descarta a prosa do modelo e cai no texto do artigo.
    # A terceira alternativa (`N pontos`) fechou a ultima brecha da medicao: "sao
    # necessarios 700 pontos" comunica o limiar inteiro **sem usar a palavra score**.
    # Enumerar sinonimo por sinonimo seria a corrida perdida de novo; o que funciona e
    # cobrir a forma em que o numero aparece.
    (
        "limiar_de_score",
        re.compile(r"\bscore\b.{0,60}\b\d{3}\b|\b\d{3}\b.{0,60}\bscore\b|\b\d{3}\s+pontos?\b"),
    ),
    # Alcada de aprovacao: diz a quem insistir e ate quanto.
    ("alcada_de_aprovacao", re.compile(r"\balcada\b|\bgerente\s+(?:pode|aprova)\b")),
    # Peso de fator no modelo: permite otimizar o que mais pontua.
    (
        "peso_de_fator",
        re.compile(r"\b(peso|pondera[cç]ao)\b.{0,30}\d{1,3}\s*%|\b\d{1,3}\s*%\s+do\s+score\b"),
    ),
    # Teto de comprometimento: diz qual valor pedir para passar.
    (
        "teto_de_comprometimento",
        re.compile(
            r"\bcomprometimento\b.{0,40}\b(acima|superior|limite|teto|maximo|vedad[oa])\b"
            r".{0,15}\d{1,3}\s*%"
        ),
    ),
    # Nome de sistema ou tabela interna.
    (
        "sistema_interno",
        re.compile(r"\b(pgvector|scoring\.py|esteira\s+interna|tabela\s+trecho)\b"),
    ),
)


@dataclass(frozen=True, slots=True)
class Veredito:
    """Resultado da inspecao de uma resposta antes de ela sair."""

    liberada: bool
    vazamentos: tuple[str, ...] = field(default=())

    @property
    def bloqueada(self) -> bool:
        return not self.liberada


def _normalizar(texto: str) -> str:
    sem_acento = "".join(
        c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c)
    )
    return " ".join(sem_acento.lower().split())


def inspecionar(resposta: str) -> Veredito:
    """Verifica se a resposta pode ir para o cliente.

    Roda sobre o texto **normalizado** (sem acento, minusculo, espaco colapsado): um
    padrao que so casasse a forma acentuada seria contornado por qualquer variacao de
    escrita do modelo, e o modelo varia.
    """
    texto = _normalizar(resposta)
    encontrados = tuple(nome for nome, padrao in _PADROES if padrao.search(texto))

    return Veredito(liberada=not encontrados, vazamentos=encontrados)


def descrever_padroes() -> tuple[str, ...]:
    """Nomes dos padroes verificados — usado no teste e na documentacao da API."""
    return tuple(nome for nome, _ in _PADROES)
