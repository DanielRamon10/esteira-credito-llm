"""Testes do carregamento da base."""

from __future__ import annotations

from pathlib import Path

import pytest

from customer_support.domain.divulgacao import Visibilidade
from customer_support.infrastructure.conhecimento import ConhecimentoEmArquivos


def escrever(diretorio: Path, nome: str, visibilidade: str, corpo: str) -> None:
    (diretorio / nome).write_text(
        f'---\nid: {nome[:-3]}\ntitulo: "Titulo de {nome}"\n'
        f"visibilidade: {visibilidade}\n---\n{corpo}\n",
        encoding="utf-8",
    )


class TestCarregamento:
    def test_le_e_separa_por_visibilidade(self, tmp_path: Path) -> None:
        escrever(tmp_path, "publico.md", "publica", "Texto publico sobre renda.")
        escrever(tmp_path, "interno.md", "interna", "Score acima de 700.")

        base = ConhecimentoEmArquivos(tmp_path)

        assert base.total == 2
        assert base.publicos == 1

    def test_busca_do_cliente_nao_alcanca_interno(self, tmp_path: Path) -> None:
        """A defesa mais importante deste servico, no nivel do adapter."""
        escrever(tmp_path, "publico.md", "publica", "Como comprovar renda com holerite.")
        escrever(tmp_path, "interno.md", "interna", "O score minimo de aprovacao e 700 pontos.")

        base = ConhecimentoEmArquivos(tmp_path)

        resultados = base.buscar("score minimo de aprovacao", k=5, apenas_publicos=True)

        assert all(r.artigo.visibilidade is Visibilidade.PUBLICA for r in resultados)
        assert "interno" not in [r.artigo.id for r in resultados]

    def test_busca_completa_existe_para_uso_interno(self, tmp_path: Path) -> None:
        escrever(tmp_path, "publico.md", "publica", "Renda e holerite.")
        escrever(tmp_path, "interno.md", "interna", "Score minimo 700.")

        base = ConhecimentoEmArquivos(tmp_path)

        assert len(base.buscar("score", k=5, apenas_publicos=False)) >= 1

    def test_produtos_do_front_matter(self, tmp_path: Path) -> None:
        (tmp_path / "a.md").write_text(
            "---\n"
            "id: a\n"
            'titulo: "A"\n'
            "visibilidade: publica\n"
            "produtos: cdc, consignado\n"
            "---\n"
            "Texto.\n",
            encoding="utf-8",
        )

        artigo = ConhecimentoEmArquivos(tmp_path).todos()[0]

        assert artigo.produtos == frozenset({"cdc", "consignado"})


class TestFalhaAlto:
    def test_diretorio_inexistente(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError, match="nao encontrado"):
            ConhecimentoEmArquivos(tmp_path / "nao-existe")

    def test_diretorio_vazio(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError, match="Nenhum artigo"):
            ConhecimentoEmArquivos(tmp_path)

    def test_visibilidade_invalida_derruba(self, tmp_path: Path) -> None:
        """Nao cair no default publico.

        Um artigo interno com valor digitado errado seria servido ao cliente — e o
        erro passaria como configuracao valida.
        """
        escrever(tmp_path, "a.md", "interno", "Score minimo 700.")

        with pytest.raises(RuntimeError, match="invalida"):
            ConhecimentoEmArquivos(tmp_path)

    def test_arquivo_sem_front_matter_e_ignorado(self, tmp_path: Path) -> None:
        (tmp_path / "sem.md").write_text("Só texto, sem front-matter.\n", encoding="utf-8")
        escrever(tmp_path, "ok.md", "publica", "Texto valido.")

        assert ConhecimentoEmArquivos(tmp_path).total == 1
