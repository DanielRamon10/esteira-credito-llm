"""Carregamento e busca na base de conhecimento.

O indice BM25 vem de `plataforma.bm25` — o mesmo do `credit-analysis`. Foi
justamente este servico que motivou a extracao da biblioteca: copiar o algoritmo
pela terceira vez seria indefensavel.
"""

from __future__ import annotations

import re
from pathlib import Path

import structlog
from plataforma.bm25 import IndiceBM25

from customer_support.domain.conhecimento import Artigo, ArtigoRecuperado
from customer_support.domain.divulgacao import Visibilidade

logger = structlog.get_logger(__name__)

# Front-matter YAML simples. Nao usa PyYAML de proposito: sao quatro campos de
# valor escalar, e uma dependencia a mais num servico cujo argumento e ser leve
# custa mais do que quinze linhas de parsing.
_FRONT_MATTER = re.compile(r"\A\s*---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL)


class ConhecimentoEmArquivos:
    """Le os artigos do disco uma vez, na construcao."""

    def __init__(self, diretorio: Path) -> None:
        self._diretorio = diretorio
        self._artigos: list[Artigo] = []

        if not diretorio.is_dir():
            raise RuntimeError(
                f"Diretorio de conhecimento nao encontrado: {diretorio}. "
                "Sem base o servico responderia toda duvida com 'nao sei', o que e "
                "pior que nao responder: o cliente conclui que a informacao nao existe."
            )

        for caminho in sorted(diretorio.glob("*.md")):
            artigo = self._ler(caminho)
            if artigo is not None:
                self._artigos.append(artigo)

        if not self._artigos:
            raise RuntimeError(f"Nenhum artigo carregado de {diretorio}")

        # Dois indices: um so com publicos (o que a busca do cliente usa) e um com
        # tudo (usado apenas por teste e por ferramenta interna). Separar na
        # construcao e mais seguro que filtrar no resultado — o segundo caminho
        # depende de ninguem esquecer o filtro.
        self._publicos = [a for a in self._artigos if a.publico]
        self._indice_publico = IndiceBM25([a.texto_para_indexar for a in self._publicos])
        self._indice_completo = IndiceBM25([a.texto_para_indexar for a in self._artigos])

        logger.info(
            "conhecimento.carregado",
            artigos=len(self._artigos),
            publicos=len(self._publicos),
            internos=len(self._artigos) - len(self._publicos),
        )

    @staticmethod
    def _ler(caminho: Path) -> Artigo | None:
        bruto = caminho.read_text(encoding="utf-8").lstrip("\ufeff")
        casado = _FRONT_MATTER.match(bruto)
        if casado is None:
            logger.warning("conhecimento.sem_front_matter", arquivo=caminho.name)
            return None

        campos: dict[str, str] = {}
        for linha in casado.group(1).splitlines():
            if ":" not in linha:
                continue
            chave, _, valor = linha.partition(":")
            campos[chave.strip()] = valor.strip().strip('"')

        bruta = campos.get("visibilidade", "publica")
        try:
            visibilidade = Visibilidade(bruta)
        except ValueError as exc:
            # Falha alta: um artigo interno marcado com valor invalido cairia no
            # default publico e seria servido ao cliente.
            raise RuntimeError(
                f"{caminho.name}: visibilidade '{bruta}' invalida. Use 'publica' ou 'interna'."
            ) from exc

        produtos = campos.get("produtos", "")
        return Artigo(
            id=campos.get("id", caminho.stem),
            titulo=campos.get("titulo", caminho.stem),
            texto=casado.group(2).strip(),
            visibilidade=visibilidade,
            produtos=frozenset(p.strip() for p in produtos.split(",") if p.strip()),
            atualizado_em=campos.get("atualizado_em", ""),
        )

    def buscar(
        self, pergunta: str, k: int = 3, apenas_publicos: bool = True
    ) -> list[ArtigoRecuperado]:
        fonte = self._publicos if apenas_publicos else self._artigos
        indice = self._indice_publico if apenas_publicos else self._indice_completo

        if not pergunta.strip() or k <= 0:
            return []

        return [
            ArtigoRecuperado(artigo=fonte[item.indice], score=item.score)
            for item in indice.buscar(pergunta, k=k)
        ]

    def todos(self) -> list[Artigo]:
        return list(self._artigos)

    @property
    def total(self) -> int:
        return len(self._artigos)

    @property
    def publicos(self) -> int:
        return len(self._publicos)

    @property
    def procedencia(self) -> str:
        return f"{self._diretorio.name}:{len(self._artigos)} artigos"
