"""Vector store em memoria.

Implementa `RepositorioPoliticas` com uma matriz NumPy e similaridade de
cosseno. Para um corpus de politicas internas — dezenas a poucos milhares de
trechos — busca exata em matriz densa e mais rapida que qualquer indice
aproximado, e nao tem o custo de manter um HNSW. O adapter pgvector existe
para quando o corpus deixar de caber confortavelmente em memoria e para
compartilhar o indice entre replicas.

Os vetores sao normalizados na indexacao, entao a similaridade de cosseno vira
um unico produto matriz-vetor.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import numpy.typing as npt

from credit_analysis.domain.politica import TrechoPolitica, TrechoRecuperado


class VectorStoreMemoria:
    """Indice denso in-process."""

    def __init__(self) -> None:
        self._trechos: list[TrechoPolitica] = []
        self._indice_por_id: dict[str, int] = {}
        self._matriz: npt.NDArray[np.float32] = np.zeros((0, 0), dtype=np.float32)

    async def indexar(
        self, trechos: Sequence[TrechoPolitica], vetores: Sequence[Sequence[float]]
    ) -> None:
        if len(trechos) != len(vetores):
            raise ValueError(
                f"Quantidade de trechos ({len(trechos)}) difere da de vetores ({len(vetores)})"
            )
        if not trechos:
            return

        novos = np.asarray(vetores, dtype=np.float32)
        if novos.ndim != 2:
            raise ValueError("Vetores devem formar uma matriz 2-D")

        if self._matriz.size and novos.shape[1] != self._matriz.shape[1]:
            raise ValueError(
                f"Dimensao {novos.shape[1]} incompativel com o indice "
                f"({self._matriz.shape[1]}). Reindexe o corpus ao trocar de modelo."
            )

        novos = _normalizar(novos)

        # Reindexacao de um trecho ja presente sobrescreve em vez de duplicar:
        # rodar a ingestao duas vezes nao pode inflar os resultados.
        linhas_novas: list[npt.NDArray[np.float32]] = []
        trechos_novos: list[TrechoPolitica] = []

        for trecho, vetor in zip(trechos, novos, strict=True):
            existente = self._indice_por_id.get(trecho.id)
            if existente is None:
                self._indice_por_id[trecho.id] = len(self._trechos) + len(trechos_novos)
                trechos_novos.append(trecho)
                linhas_novas.append(vetor)
            else:
                self._trechos[existente] = trecho
                self._matriz[existente] = vetor

        if trechos_novos:
            self._trechos.extend(trechos_novos)
            bloco = np.vstack(linhas_novas)
            self._matriz = bloco if self._matriz.size == 0 else np.vstack([self._matriz, bloco])

    async def buscar_denso(
        self,
        vetor: Sequence[float],
        k: int = 5,
        produto: str | None = None,
    ) -> list[TrechoRecuperado]:
        if not self._trechos or k <= 0:
            return []

        consulta = _normalizar(np.asarray([vetor], dtype=np.float32))[0]
        similaridades = self._matriz @ consulta

        candidatos = [
            i for i, t in enumerate(self._trechos) if produto is None or t.aplicavel_a(produto)
        ]
        if not candidatos:
            return []

        indices = np.asarray(candidatos)
        scores = similaridades[indices]

        # argpartition evita ordenar o indice inteiro quando k << n.
        k_efetivo = min(k, len(indices))
        melhores = indices[np.argpartition(-scores, k_efetivo - 1)[:k_efetivo]]
        melhores = melhores[np.argsort(-similaridades[melhores])]

        return [
            TrechoRecuperado(
                trecho=self._trechos[i],
                score=float(similaridades[i]),
                origem="denso",
            )
            for i in melhores
        ]

    async def listar_todos(self) -> list[TrechoPolitica]:
        return list(self._trechos)

    async def contar(self) -> int:
        return len(self._trechos)

    def limpar(self) -> None:
        """Reset entre testes. Fora do port: nao e operacao de negocio."""
        self._trechos.clear()
        self._indice_por_id.clear()
        self._matriz = np.zeros((0, 0), dtype=np.float32)


def _normalizar(matriz: npt.NDArray[np.float32]) -> npt.NDArray[np.float32]:
    """Normaliza cada linha para norma 1, deixando linhas nulas intactas."""
    normas = np.linalg.norm(matriz, axis=1, keepdims=True)
    normas[normas == 0] = 1.0
    return (matriz / normas).astype(np.float32)
