"""Indice lexical BM25.

Implementado aqui em vez de importar `rank-bm25` por dois motivos praticos: a
tokenizacao precisa entender portugues (acento, numero de resolucao, codigo
"POL-001") e o algoritmo inteiro cabe em 60 linhas. Uma dependencia a menos
num servico que ja carrega ONNX e Postgres.

BM25 pontua um documento pela soma, sobre os termos da consulta, de:

    IDF(termo) * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * |D| / avgdl))

O `tf` saturante e o que diferencia BM25 de TF-IDF: a decima ocorrencia de um
termo vale muito menos que a segunda. `b` controla quanto o tamanho do
documento penaliza a pontuacao.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

# Valores classicos de Robertson. k1 controla a saturacao do tf; b, a
# normalizacao por tamanho.
K1 = 1.5
B = 0.75

# Mantem letras, digitos e o hifen interno de codigos como "POL-001" e
# "e-social". Split ingenuo por espaco quebraria "POL-001" em nada util e
# perderia o termo mais discriminativo do corpus.
_TOKEN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")

# Palavras funcionais do portugues. Nao removemos numeros nem termos de
# dominio — "50", "4966" e "consignado" sao exatamente o que da sinal aqui.
_STOPWORDS = frozenset(
    [
        "a",
        "as",
        "o",
        "os",
        "um",
        "uma",
        "uns",
        "umas",
        "de",
        "do",
        "da",
        "dos",
        "das",
        "em",
        "no",
        "na",
        "nos",
        "nas",
        "por",
        "para",
        "com",
        "sem",
        "sob",
        "sobre",
        "entre",
        "ate",
        "apos",
        "ante",
        "e",
        "ou",
        "mas",
        "que",
        "se",
        "como",
        "quando",
        "onde",
        "qual",
        "quais",
        "quanto",
        "quanta",
        "ser",
        "estar",
        "ter",
        "haver",
        "e_",
        "do_",
        "ao",
        "aos",
        "a_",
        "pelo",
        "pela",
        "pelos",
        "pelas",
        "este",
        "esta",
        "estes",
        "estas",
        "esse",
        "essa",
        "esses",
        "essas",
        "aquele",
        "aquela",
        "isso",
        "isto",
        "seu",
        "sua",
        "seus",
        "suas",
        "nao",
        "sim",
        "ja",
        "mais",
        "menos",
        "muito",
        "pouco",
        "todo",
        "toda",
        "todos",
        "todas",
    ]
)


def tokenizar(texto: str) -> list[str]:
    """Normaliza acentos, minuscula e remove stopwords.

    Remover acento e deliberado: o corpus escreve "vigencia" e o usuario
    digita "vigência" (ou o contrario). Sem normalizar, os dois nunca casam.
    """
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return [t for t in _TOKEN.findall(sem_acento.lower()) if t not in _STOPWORDS]


@dataclass(frozen=True, slots=True)
class ResultadoLexical:
    """Posicao de um documento no ranking lexical."""

    indice: int
    score: float


class IndiceBM25:
    """Indice invertido BM25 sobre uma colecao fixa de documentos."""

    def __init__(self, documentos: Sequence[str]) -> None:
        self._tokens: list[list[str]] = [tokenizar(d) for d in documentos]
        self._tamanhos: list[int] = [len(t) for t in self._tokens]
        self._total = len(self._tokens)
        self._tamanho_medio = (sum(self._tamanhos) / self._total) if self._total else 0.0

        # frequencia do termo por documento + em quantos documentos ele aparece
        self._frequencias: list[Counter[str]] = [Counter(t) for t in self._tokens]
        documentos_por_termo: Counter[str] = Counter()
        for tokens in self._tokens:
            documentos_por_termo.update(set(tokens))

        # IDF de Robertson com suavizacao: sempre positivo, mesmo para termo
        # presente em todos os documentos.
        self._idf: dict[str, float] = {
            termo: math.log(1 + (self._total - n + 0.5) / (n + 0.5))
            for termo, n in documentos_por_termo.items()
        }

    def __len__(self) -> int:
        return self._total

    def buscar(self, consulta: str, k: int = 5) -> list[ResultadoLexical]:
        """Ranking dos k documentos mais relevantes para a consulta."""
        termos = tokenizar(consulta)
        if not termos or self._total == 0:
            return []

        pontuacoes: list[float] = []
        for indice in range(self._total):
            pontuacoes.append(self._pontuar(indice, termos))

        ordenados = sorted(
            (ResultadoLexical(i, s) for i, s in enumerate(pontuacoes) if s > 0),
            key=lambda r: r.score,
            reverse=True,
        )
        return ordenados[:k]

    def _pontuar(self, indice: int, termos: Iterable[str]) -> float:
        frequencias = self._frequencias[indice]
        tamanho = self._tamanhos[indice]
        norma = K1 * (1 - B + B * tamanho / self._tamanho_medio) if self._tamanho_medio else K1

        total = 0.0
        for termo in termos:
            tf = frequencias.get(termo)
            if not tf:
                continue
            total += self._idf.get(termo, 0.0) * (tf * (K1 + 1)) / (tf + norma)
        return total
