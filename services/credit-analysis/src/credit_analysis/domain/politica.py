"""Entidades do corpus de politicas internas.

Estas entidades sao o que torna o RAG auditavel. A diferenca entre um chatbot
e uma esteira de credito e que aqui cada afirmacao precisa apontar para a
politica, a versao e a secao que a sustenta — se o parecer diz "o teto e 50%",
tem que dar para abrir POL-001 v3.2 secao 2 e conferir.

Dominio puro: nenhuma nocao de embedding, banco ou LLM aparece aqui.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date


def _slug(texto: str) -> str:
    """Normaliza um titulo de secao para compor identificador estavel."""
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", sem_acento.lower()).strip("-")


@dataclass(frozen=True, slots=True)
class ReferenciaPolitica:
    """Endereco de um trecho dentro do corpus.

    E o que vai no parecer e o que o auditor usa para localizar a fonte.
    Carrega a versao porque politica muda: um parecer emitido sob a POL-001
    v3.1 nao pode ser reavaliado contra a v3.2 sem que isso fique visivel.
    """

    politica_id: str
    versao: str
    secao: str

    def __str__(self) -> str:
        return f"{self.politica_id} v{self.versao}, {self.secao}"

    @property
    def chave(self) -> str:
        return f"{self.politica_id}@{self.versao}#{_slug(self.secao)}"


@dataclass(frozen=True, slots=True)
class TrechoPolitica:
    """Um pedaco indexavel de politica, com o contexto necessario para ser lido isolado.

    `caminho_secao` guarda a hierarquia completa de headings ("2. Faixas /
    2.1 Excecoes"). Sem isso, um trecho recuperado do meio do documento chega
    ao LLM sem saber do que esta falando.
    """

    referencia: ReferenciaPolitica
    titulo_politica: str
    caminho_secao: tuple[str, ...]
    texto: str
    produtos: frozenset[str] = field(default_factory=frozenset)
    vigencia_inicio: date | None = None
    area: str = ""

    @property
    def id(self) -> str:
        return self.referencia.chave

    @property
    def texto_para_indexar(self) -> str:
        """Texto enriquecido com o cabecalho hierarquico.

        Indexar o trecho nu perde sinal: "ate 30%" numa tabela nao casa com
        "qual o limite de comprometimento". Prefixar o titulo da politica e o
        caminho da secao devolve esse contexto ao vetor e ao indice lexical.
        """
        cabecalho = " / ".join((self.titulo_politica, *self.caminho_secao))
        return f"{cabecalho}\n\n{self.texto}"

    def vigente_em(self, momento: date) -> bool:
        return self.vigencia_inicio is None or self.vigencia_inicio <= momento

    def aplicavel_a(self, produto: str | None) -> bool:
        if produto is None or not self.produtos:
            return True
        return produto in self.produtos


@dataclass(frozen=True, slots=True)
class TrechoRecuperado:
    """Trecho devolvido pela busca, com a pontuacao e a origem do match.

    `origem` existe para depuracao: quando o retrieval traz algo estranho,
    saber se veio do lexical ou do denso aponta direto para onde olhar.
    """

    trecho: TrechoPolitica
    score: float
    origem: str

    @property
    def referencia(self) -> ReferenciaPolitica:
        return self.trecho.referencia


@dataclass(frozen=True, slots=True)
class Citacao:
    """Uma afirmacao do parecer amarrada ao trecho que a sustenta."""

    referencia: ReferenciaPolitica
    trecho_citado: str


@dataclass(frozen=True, slots=True)
class Fundamentacao:
    """Resultado da consulta ao corpus: texto + citacoes verificadas.

    `citacoes_rejeitadas` guarda o que o modelo alegou mas nao pode ser
    confirmado nos trechos recuperados. Guardar em vez de descartar em
    silencio: e o sinal que permite medir alucinacao em producao.
    """

    texto: str
    citacoes: tuple[Citacao, ...] = ()
    citacoes_rejeitadas: tuple[str, ...] = ()
    trechos_consultados: tuple[ReferenciaPolitica, ...] = ()

    @property
    def confiavel(self) -> bool:
        """Fundamentacao sem citacao rejeitada e com ao menos uma confirmada."""
        return bool(self.citacoes) and not self.citacoes_rejeitadas
