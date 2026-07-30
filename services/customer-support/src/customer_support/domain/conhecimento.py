"""Base de conhecimento voltada ao cliente.

## Corpus proprio, e nao as politicas internas do outro servico

Seria tentador reusar o corpus de `credit-analysis` — ele ja esta indexado e
responde perguntas sobre credito. Seria errado: aquele texto e escrito para
analista, cita limiar, peso de fator e alcada. Servir isso a cliente e o vazamento
que `divulgacao.py` existe para impedir, e a melhor forma de nao vazar e nao ter o
dado por perto.

Entao a base aqui e escrita para cliente, do zero, e cada artigo declara sua
`visibilidade`. Os artigos internos existem no corpus **de proposito**: eles sao o
que o guard de saida tem para comparar, e sem eles o teste de vazamento nao teria
material realista.

## Sem embedding, e isso e decisao medida

O `credit-analysis` usa busca hibrida com modelo de 2,24GB porque o corpus dele e
grande, tecnico e cheio de parafrase. Aqui sao poucas dezenas de artigos curtos, e a
medicao em `tests/eval` mostra BM25 sozinho resolvendo — entao o servico nao carrega
ONNX, nao paga cold start de 5,8s e cabe numa imagem pequena, como o `kyc`.

E a mesma disciplina das outras escolhas do projeto: a ferramenta mais pesada so
entra quando a medicao mostra que a leve nao serve.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from customer_support.domain.divulgacao import Visibilidade


@dataclass(frozen=True, slots=True)
class Artigo:
    """Um artigo da base de conhecimento."""

    id: str
    titulo: str
    texto: str
    visibilidade: Visibilidade = Visibilidade.PUBLICA
    produtos: frozenset[str] = field(default=frozenset())
    atualizado_em: str = ""

    @property
    def publico(self) -> bool:
        return self.visibilidade is Visibilidade.PUBLICA

    @property
    def texto_para_indexar(self) -> str:
        """Titulo mais corpo.

        O titulo entra no indice porque em FAQ ele carrega os termos que a pessoa
        usa na pergunta ("portabilidade", "antecipar parcela") com mais densidade que
        o corpo.
        """
        return f"{self.titulo}\n{self.texto}"


@dataclass(frozen=True, slots=True)
class ArtigoRecuperado:
    """Artigo devolvido pela busca, com a pontuacao."""

    artigo: Artigo
    score: float
