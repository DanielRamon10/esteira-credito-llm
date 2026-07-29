"""Testes do carregamento e chunking do corpus."""

from __future__ import annotations

import pathlib
import textwrap

import pytest

from credit_analysis.infrastructure.rag.carregador import (
    MAX_CARACTERES_TRECHO,
    CorpusInvalido,
    carregar_corpus,
    carregar_documento,
)

DIRETORIO_POLITICAS = pathlib.Path(__file__).parents[2] / "politicas"


FRONT_MATTER = """---
id: POL-999
titulo: Politica de Teste
versao: "1.0"
area: Testes
produtos: [cdc, consignado]
vigencia_inicio: 2025-01-01
---
"""


def escrever(
    tmp_path: pathlib.Path, nome: str, corpo: str, *, com_meta: bool = True
) -> pathlib.Path:
    """Grava um documento de politica.

    O corpo passa por dedent para que os testes possam escrever markdown
    indentado dentro da classe; o front-matter e prefixado depois, ja que
    dedent sobre a concatenacao nao encontraria prefixo comum.
    """
    conteudo = textwrap.dedent(corpo).lstrip()
    caminho = tmp_path / nome
    caminho.write_text((FRONT_MATTER + "\n" + conteudo) if com_meta else conteudo, encoding="utf-8")
    return caminho


class TestFrontMatter:
    def test_extrai_metadados(self, tmp_path: pathlib.Path) -> None:
        arquivo = escrever(tmp_path, "p.md", "\n# Titulo\n\n## 1. Secao\n\n" + "x" * 200)
        trecho = carregar_documento(arquivo)[0]

        assert trecho.referencia.politica_id == "POL-999"
        assert trecho.referencia.versao == "1.0"
        assert trecho.titulo_politica == "Politica de Teste"
        assert trecho.produtos == frozenset({"cdc", "consignado"})
        assert trecho.area == "Testes"

    def test_tolera_linha_em_branco_antes_do_delimitador(self, tmp_path: pathlib.Path) -> None:
        # Regressao: um editor inseriu "\n" no topo de POL-001 e o corpus
        # inteiro parou de carregar. Ruido de formatacao nao pode invalidar
        # documento.
        arquivo = escrever(
            tmp_path,
            "p.md",
            "\n\n" + FRONT_MATTER + "\n# T\n\n## 1. S\n\n" + "x" * 200,
            com_meta=False,
        )
        assert carregar_documento(arquivo)[0].referencia.politica_id == "POL-999"

    def test_tolera_bom_utf8(self, tmp_path: pathlib.Path) -> None:
        # O Windows produz arquivo com BOM com facilidade; `encoding="utf-8"`
        # nao o remove.
        arquivo = escrever(
            tmp_path,
            "p.md",
            "﻿" + FRONT_MATTER + "\n# T\n\n## 1. S\n\n" + "x" * 200,
            com_meta=False,
        )
        assert carregar_documento(arquivo)[0].referencia.politica_id == "POL-999"

    def test_sem_front_matter_falha(self, tmp_path: pathlib.Path) -> None:
        arquivo = escrever(tmp_path, "p.md", "# Sem metadados\n\n" + "x" * 200, com_meta=False)
        with pytest.raises(CorpusInvalido, match="front-matter"):
            carregar_documento(arquivo)

    def test_campo_obrigatorio_ausente_falha(self, tmp_path: pathlib.Path) -> None:
        arquivo = escrever(
            tmp_path,
            "p.md",
            "---\nid: POL-1\ntitulo: X\n---\n\n" + "y" * 200,
            com_meta=False,
        )
        with pytest.raises(CorpusInvalido, match="versao"):
            carregar_documento(arquivo)


class TestHierarquiaDeSecoes:
    def test_headings_de_mesmo_nivel_sao_irmaos(self, tmp_path: pathlib.Path) -> None:
        # Regressao: a pilha de headings aninhava "## 2." dentro de "## 1.",
        # produzindo referencias erradas e poluindo o texto indexado.
        arquivo = escrever(
            tmp_path,
            "p.md",
            f"""
            # Titulo

            ## 1. Primeira

            {"a" * 200}

            ## 2. Segunda

            {"b" * 200}
            """,
        )
        secoes = [t.referencia.secao for t in carregar_documento(arquivo)]
        assert secoes == ["1. Primeira", "2. Segunda"]

    def test_subsecao_carrega_o_caminho_do_pai(self, tmp_path: pathlib.Path) -> None:
        arquivo = escrever(
            tmp_path,
            "p.md",
            f"""
            # Titulo

            ## 3. Pai

            {"a" * 200}

            ### 3.1 Filha

            {"b" * 200}
            """,
        )
        trechos = carregar_documento(arquivo)
        assert trechos[1].referencia.secao == "3. Pai / 3.1 Filha"
        assert trechos[1].caminho_secao == ("3. Pai", "3.1 Filha")

    def test_volta_de_nivel_descarta_o_ramo(self, tmp_path: pathlib.Path) -> None:
        arquivo = escrever(
            tmp_path,
            "p.md",
            f"""
            # Titulo

            ## 1. Pai

            {"a" * 200}

            ### 1.1 Filha

            {"b" * 200}

            ## 2. Tio

            {"c" * 200}
            """,
        )
        secoes = [t.referencia.secao for t in carregar_documento(arquivo)]
        assert secoes == ["1. Pai", "1. Pai / 1.1 Filha", "2. Tio"]


class TestChunking:
    def test_secao_curta_e_mesclada(self, tmp_path: pathlib.Path) -> None:
        # Um heading com uma frase vira ruido isolado no indice.
        arquivo = escrever(
            tmp_path,
            "p.md",
            f"""
            # Titulo

            ## 1. Curta

            Uma frase.

            ## 2. Longa

            {"z" * 300}
            """,
        )
        trechos = carregar_documento(arquivo)
        assert len(trechos) == 1
        assert "Uma frase." in trechos[0].texto
        assert "z" * 300 in trechos[0].texto

    def test_secao_longa_e_dividida_com_sufixo_de_parte(self, tmp_path: pathlib.Path) -> None:
        paragrafo = "palavra " * 120  # ~960 chars
        corpo = "\n\n".join([paragrafo] * 4)  # bem acima do limite
        arquivo = escrever(tmp_path, "p.md", f"\n# Titulo\n\n## 1. Grande\n\n{corpo}\n")
        trechos = carregar_documento(arquivo)

        assert len(trechos) > 1
        assert all("(parte " in t.referencia.secao for t in trechos)
        assert all(len(t.texto) <= MAX_CARACTERES_TRECHO * 1.2 for t in trechos)

    def test_tabela_nao_e_partida_ao_meio(self, tmp_path: pathlib.Path) -> None:
        # Tabela markdown nao tem linha em branco interna, entao a quebra por
        # paragrafo a trata como bloco unico.
        linhas = "\n".join(f"| item {i} | valor {i} |" for i in range(60))
        tabela = f"| a | b |\n|---|---|\n{linhas}"
        arquivo = escrever(
            tmp_path,
            "p.md",
            f"\n# T\n\n## 1. Com tabela\n\n{'x' * 1500}\n\n{tabela}\n",
        )
        trechos = carregar_documento(arquivo)
        com_tabela = [t for t in trechos if "| item 0 |" in t.texto]

        assert len(com_tabela) == 1
        assert "| item 59 |" in com_tabela[0].texto

    def test_secao_sem_heading_recebe_rotulo_padrao(self, tmp_path: pathlib.Path) -> None:
        arquivo = escrever(tmp_path, "p.md", f"\n# Titulo\n\n{'x' * 300}\n")
        assert carregar_documento(arquivo)[0].referencia.secao == "Introducao"


class TestTextoParaIndexar:
    def test_inclui_titulo_e_caminho(self, tmp_path: pathlib.Path) -> None:
        # Sem o cabecalho, "ate 30%" numa tabela nao casa com a pergunta.
        arquivo = escrever(
            tmp_path,
            "p.md",
            f"\n# T\n\n## 2. Faixas\n\n### 2.1 Teto\n\n{'x' * 200}\n",
        )
        trecho = next(t for t in carregar_documento(arquivo) if "2.1" in t.referencia.secao)
        indexado = trecho.texto_para_indexar

        assert indexado.startswith("Politica de Teste / 2. Faixas / 2.1 Teto")
        assert trecho.texto in indexado


class TestCorpusReal:
    def test_carrega_todas_as_politicas(self) -> None:
        trechos = carregar_corpus(DIRETORIO_POLITICAS)
        politicas = {t.referencia.politica_id for t in trechos}
        assert politicas == {f"POL-00{i}" for i in range(1, 7)}

    def test_ids_de_trecho_sao_unicos(self) -> None:
        # Id repetido faria o vector store sobrescrever trecho legitimo.
        trechos = carregar_corpus(DIRETORIO_POLITICAS)
        ids = [t.id for t in trechos]
        assert len(ids) == len(set(ids))

    def test_ordem_e_estavel_entre_execucoes(self) -> None:
        primeira = [t.id for t in carregar_corpus(DIRETORIO_POLITICAS)]
        segunda = [t.id for t in carregar_corpus(DIRETORIO_POLITICAS)]
        assert primeira == segunda

    def test_todo_trecho_tem_texto_util(self) -> None:
        for trecho in carregar_corpus(DIRETORIO_POLITICAS):
            assert len(trecho.texto.strip()) >= 50, trecho.referencia

    def test_diretorio_inexistente_falha(self, tmp_path: pathlib.Path) -> None:
        with pytest.raises(CorpusInvalido, match="nao encontrado"):
            carregar_corpus(tmp_path / "nao-existe")

    def test_diretorio_vazio_falha(self, tmp_path: pathlib.Path) -> None:
        with pytest.raises(CorpusInvalido, match="Nenhuma politica"):
            carregar_corpus(tmp_path)
