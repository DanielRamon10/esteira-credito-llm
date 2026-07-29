"""Carregamento e chunking do corpus de politicas.

O chunking e **consciente da estrutura do markdown**, nao de tamanho fixo.
Cortar a cada N tokens parte tabela no meio: metade das faixas de
comprometimento fica num chunk e metade em outro, e nenhum dos dois responde
"qual o teto?". Aqui o corte respeita headings, e a hierarquia de titulos vai
junto no metadata.

Secoes grandes demais ainda precisam ser divididas — nesse caso a quebra e por
paragrafo, com o cabecalho da secao repetido em cada pedaco.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from credit_analysis.domain.politica import ReferenciaPolitica, TrechoPolitica

# Acima disso a secao e subdividida por paragrafo. O valor e em caracteres, nao
# tokens: o corte e estrutural e nao precisa da precisao de um tokenizer.
MAX_CARACTERES_TRECHO = 1800

# Secoes menores que isso sao mescladas com a seguinte — um heading solto com
# uma frase vira ruido no indice.
MIN_CARACTERES_TRECHO = 80

# `\s*` inicial tolera linha em branco ou BOM antes do `---`. Um editor que
# insere newline no topo nao pode invalidar o documento — e o corpus inteiro
# deixa de carregar quando um arquivo falha.
_FRONT_MATTER = re.compile(r"\A\s*---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


class CorpusInvalido(Exception):
    """Documento de politica malformado — falta front-matter ou campo obrigatorio."""


@dataclass(frozen=True, slots=True)
class MetadadosPolitica:
    """Front-matter YAML de um documento de politica."""

    id: str
    titulo: str
    versao: str
    area: str
    produtos: frozenset[str]
    vigencia_inicio: date | None

    @classmethod
    def de_dict(cls, dados: dict[str, Any], origem: Path) -> MetadadosPolitica:
        faltando = {"id", "titulo", "versao"} - dados.keys()
        if faltando:
            raise CorpusInvalido(f"{origem.name}: front-matter sem {sorted(faltando)}")

        vigencia = dados.get("vigencia_inicio")
        if isinstance(vigencia, str):
            vigencia = date.fromisoformat(vigencia)

        return cls(
            id=str(dados["id"]),
            titulo=str(dados["titulo"]),
            versao=str(dados["versao"]),
            area=str(dados.get("area", "")),
            produtos=frozenset(str(p) for p in dados.get("produtos", [])),
            vigencia_inicio=vigencia,
        )


@dataclass(slots=True)
class _SecaoBruta:
    """Secao acumulada durante o parse, antes de virar trecho."""

    caminho: tuple[str, ...]
    linhas: list[str]

    @property
    def texto(self) -> str:
        return "\n".join(self.linhas).strip()


def _extrair_front_matter(conteudo: str, origem: Path) -> tuple[dict[str, Any], str]:
    # BOM UTF-8 nao e removido por `encoding="utf-8"` e apareceria antes do
    # `---`; o Windows produz arquivo com BOM com facilidade.
    match = _FRONT_MATTER.match(conteudo.lstrip("﻿"))
    if match is None:
        raise CorpusInvalido(f"{origem.name}: documento sem front-matter YAML")

    dados = yaml.safe_load(match.group(1))
    if not isinstance(dados, dict):
        raise CorpusInvalido(f"{origem.name}: front-matter nao e um mapeamento")

    return dados, conteudo[match.end() :]


def _dividir_em_secoes(corpo: str) -> list[_SecaoBruta]:
    """Percorre o markdown mantendo a pilha de headings.

    O heading de nivel 1 e descartado do caminho: e o titulo do documento, ja
    presente no metadata, e repeti-lo em toda secao so gera ruido.
    """
    secoes: list[_SecaoBruta] = []
    pilha: list[str] = []
    atual: _SecaoBruta | None = None

    for linha in corpo.splitlines():
        cabecalho = _HEADING.match(linha)

        if cabecalho is None:
            if atual is not None:
                atual.linhas.append(linha)
            continue

        nivel = len(cabecalho.group(1))
        titulo = cabecalho.group(2)

        if atual is not None and atual.texto:
            secoes.append(atual)

        # Trunca a pilha para conter apenas os ancestrais do heading atual.
        # Como o nivel 1 (titulo do documento) fica fora da pilha, um heading
        # de nivel N ocupa a posicao N-2 — dois headings `##` sao irmaos e o
        # segundo precisa descartar o primeiro, nao aninhar-se nele.
        if nivel == 1:
            pilha.clear()
        else:
            del pilha[nivel - 2 :]
            pilha.append(titulo)

        atual = _SecaoBruta(caminho=tuple(pilha), linhas=[])

    if atual is not None and atual.texto:
        secoes.append(atual)

    return secoes


def _mesclar_curtas(secoes: list[_SecaoBruta]) -> list[_SecaoBruta]:
    """Junta secoes curtas demais com a proxima, preservando o caminho da primeira."""
    resultado: list[_SecaoBruta] = []
    pendente: _SecaoBruta | None = None

    for secao in secoes:
        if pendente is not None:
            secao = _SecaoBruta(
                caminho=pendente.caminho,
                linhas=[*pendente.linhas, "", *secao.linhas],
            )
            pendente = None

        if len(secao.texto) < MIN_CARACTERES_TRECHO:
            pendente = secao
            continue

        resultado.append(secao)

    if pendente is not None:
        # Sobrou uma secao curta no fim: anexa na anterior ou entra sozinha.
        if resultado:
            ultima = resultado[-1]
            resultado[-1] = _SecaoBruta(
                caminho=ultima.caminho, linhas=[*ultima.linhas, "", *pendente.linhas]
            )
        else:
            resultado.append(pendente)

    return resultado


def _quebrar_por_paragrafo(texto: str, limite: int) -> list[str]:
    """Divide texto longo em blocos de paragrafos sem estourar o limite.

    Uma tabela markdown e tratada como um paragrafo (nao tem linha em branco
    interna), entao ela nunca e partida ao meio por esta funcao.
    """
    paragrafos = [p for p in re.split(r"\n\s*\n", texto) if p.strip()]
    blocos: list[str] = []
    atual: list[str] = []
    tamanho = 0

    for paragrafo in paragrafos:
        if atual and tamanho + len(paragrafo) > limite:
            blocos.append("\n\n".join(atual))
            atual, tamanho = [], 0
        atual.append(paragrafo)
        tamanho += len(paragrafo) + 2

    if atual:
        blocos.append("\n\n".join(atual))

    return blocos or [texto]


def carregar_documento(caminho: Path) -> list[TrechoPolitica]:
    """Le um arquivo de politica e devolve seus trechos indexaveis."""
    conteudo = caminho.read_text(encoding="utf-8")
    dados, corpo = _extrair_front_matter(conteudo, caminho)
    meta = MetadadosPolitica.de_dict(dados, caminho)

    trechos: list[TrechoPolitica] = []

    for secao in _mesclar_curtas(_dividir_em_secoes(corpo)):
        rotulo = " / ".join(secao.caminho) or "Introducao"
        blocos = _quebrar_por_paragrafo(secao.texto, MAX_CARACTERES_TRECHO)

        for indice, bloco in enumerate(blocos):
            # Sufixo so quando a secao foi realmente dividida: mantem a
            # referencia limpa no caso comum.
            sufixo = f" (parte {indice + 1})" if len(blocos) > 1 else ""
            trechos.append(
                TrechoPolitica(
                    referencia=ReferenciaPolitica(
                        politica_id=meta.id,
                        versao=meta.versao,
                        secao=rotulo + sufixo,
                    ),
                    titulo_politica=meta.titulo,
                    caminho_secao=secao.caminho,
                    texto=bloco,
                    produtos=meta.produtos,
                    vigencia_inicio=meta.vigencia_inicio,
                    area=meta.area,
                )
            )

    return trechos


def carregar_corpus(diretorio: Path) -> list[TrechoPolitica]:
    """Carrega todos os `.md` do diretorio, em ordem estavel de nome.

    Ordem estavel importa: o indice BM25 e os ids de trecho ficam
    reproduzíveis entre execucoes, o que torna os testes deterministicos.
    """
    if not diretorio.is_dir():
        raise CorpusInvalido(f"Diretorio de politicas nao encontrado: {diretorio}")

    trechos: list[TrechoPolitica] = []
    for arquivo in sorted(diretorio.glob("*.md")):
        trechos.extend(carregar_documento(arquivo))

    if not trechos:
        raise CorpusInvalido(f"Nenhuma politica encontrada em {diretorio}")

    return trechos
