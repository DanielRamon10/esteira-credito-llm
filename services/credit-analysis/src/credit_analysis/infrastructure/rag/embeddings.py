"""Adapters de embedding.

Dois adapters implementam o port `Embedder`:

- `EmbedderFastEmbed` — modelo ONNX rodando local, sem chamada de rede e sem
  custo por token. Para politicas internas isso nao e so economia: o corpus
  nao sai da maquina.
- `EmbedderFake` — vetor deterministico derivado de hash. A suite de testes
  roda em milissegundos sem baixar 220MB de modelo, e os testes de retrieval
  continuam validos porque so dependem de o vetor ser estavel por texto.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from functools import cached_property
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - apenas para tipagem
    from fastembed import TextEmbedding

# Escolhido por medicao, nao por tamanho. Sobre o corpus de politicas
# (37 trechos, 20 perguntas, acerto medido no nivel de SECAO):
#
#   modelo                                    top-1    top-3
#   paraphrase-multilingual-MiniLM (384d)     55,0%    75,0%
#   multilingual-e5-large (1024d)             90,0%    95,0%
#
# A diferenca nao e so tamanho: `paraphrase-*` e treinado para similaridade
# SIMETRICA (frase vs frase), e RAG e ASSIMETRICO (pergunta curta vs passagem
# longa). A familia E5 e treinada para o segundo caso. Usar um modelo de
# paraphrase em retrieval e um erro de categoria que aparece como "o RAG esta
# ruim" sem causa obvia.
#
# Custo: 2,24GB baixados uma vez, contra 220MB. Para o valor entregue, vale.
MODELO_PADRAO = "intfloat/multilingual-e5-large"

# Alternativa leve, para maquina sem espaco. Perde qualidade — ver medicao acima.
MODELO_LEVE = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

# Modelos E5 e BGE foram treinados com prefixos distintos para consulta e
# documento; omiti-los degrada a busca silenciosamente. O `query_embed` do
# fastembed NAO adiciona esses prefixos (verificado: `query_embed(t)` devolve
# vetor identico a `embed(t)`), entao aplicamos aqui, explicitamente.
_PREFIXOS: dict[str, tuple[str, str]] = {
    "intfloat/multilingual-e5-large": ("query: ", "passage: "),
    "intfloat/multilingual-e5-small": ("query: ", "passage: "),
    "intfloat/multilingual-e5-base": ("query: ", "passage: "),
}


class EmbedderFastEmbed:
    """Embeddings locais via ONNX Runtime."""

    def __init__(self, modelo: str = MODELO_PADRAO) -> None:
        self._nome = modelo
        self._prefixo_consulta, self._prefixo_documento = _PREFIXOS.get(modelo, ("", ""))

    @cached_property
    def _modelo(self) -> TextEmbedding:
        # Carregado sob demanda: importar fastembed puxa onnxruntime e o
        # primeiro uso baixa o modelo. Fazer isso no __init__ tornaria o import
        # do modulo lento e quebraria testes que so precisam do fake.
        from fastembed import TextEmbedding

        return TextEmbedding(self._nome)

    @property
    def dimensoes(self) -> int:
        for spec in type(self._modelo).list_supported_models():
            if spec["model"] == self._nome:
                return int(spec["dim"])
        raise ValueError(f"Modelo desconhecido: {self._nome}")

    @property
    def identificacao(self) -> str:
        return self._nome

    def vetorizar(self, textos: Sequence[str]) -> list[list[float]]:
        if not textos:
            return []
        entradas = [self._prefixo_documento + t for t in textos]
        return [vetor.tolist() for vetor in self._modelo.embed(entradas)]

    def vetorizar_consulta(self, texto: str) -> list[float]:
        # Usa `embed` e nao `query_embed`: no fastembed os dois produzem o
        # mesmo vetor, e depender do helper daria a falsa impressao de que ele
        # cuida do prefixo. O prefixo e nosso, e fica visivel aqui.
        entrada = self._prefixo_consulta + texto
        vetor: list[float] = next(iter(self._modelo.embed([entrada]))).tolist()
        return vetor


class EmbedderFake:
    """Embedder deterministico para testes.

    Projeta o texto num espaco de `dimensoes` via hashing de trigramas de
    caracteres. Nao captura semantica — mas captura sobreposicao lexical, que
    e o suficiente para verificar que o pipeline de indexacao e busca liga as
    pontas certas. Testes de qualidade semantica usam o modelo real e ficam
    marcados como lentos.
    """

    def __init__(self, dimensoes: int = 64) -> None:
        self._dimensoes = dimensoes

    @property
    def dimensoes(self) -> int:
        return self._dimensoes

    @property
    def identificacao(self) -> str:
        return f"fake-{self._dimensoes}d"

    def _vetor(self, texto: str) -> list[float]:
        vetor = [0.0] * self._dimensoes
        normalizado = texto.lower()

        for i in range(max(1, len(normalizado) - 2)):
            trigrama = normalizado[i : i + 3]
            digest = hashlib.blake2b(trigrama.encode(), digest_size=8).digest()
            indice = int.from_bytes(digest[:4], "big") % self._dimensoes
            sinal = 1.0 if digest[4] & 1 else -1.0
            vetor[indice] += sinal

        norma = math.sqrt(sum(v * v for v in vetor))
        return [v / norma for v in vetor] if norma else vetor

    def vetorizar(self, textos: Sequence[str]) -> list[list[float]]:
        return [self._vetor(t) for t in textos]

    def vetorizar_consulta(self, texto: str) -> list[float]:
        return self._vetor(texto)
