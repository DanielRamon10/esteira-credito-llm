"""Testes do carregamento de listas."""

from __future__ import annotations

from pathlib import Path

import pytest

from kyc_compliance.domain.triagem import TipoLista
from kyc_compliance.infrastructure.listas import ListasDeArquivo, ListasEmMemoria


def escrever(diretorio: Path, nome: str, linhas: list[str]) -> None:
    (diretorio / nome).write_text(
        "nome,tipo,origem,cpf,cargo,observacao\n" + "\n".join(linhas) + "\n",
        encoding="utf-8",
    )


class TestCarregamento:
    def test_le_varios_arquivos(self, tmp_path: Path) -> None:
        escrever(tmp_path, "a.csv", ["JOSE SILVA,pep,fonte-a,,Prefeito,"])
        escrever(tmp_path, "b.csv", ["MARIA SOUZA,sancao,fonte-b,,,"])

        listas = ListasDeArquivo(tmp_path)

        assert listas.total == 2
        assert {e.tipo for e in listas.todas()} == {TipoLista.PEP, TipoLista.SANCAO}

    def test_procedencia_registra_os_arquivos(self, tmp_path: Path) -> None:
        # Vai para a trilha de auditoria: "contra qual versao da lista foi triado".
        escrever(tmp_path, "sancoes.csv", ["JOSE SILVA,sancao,fonte,,,"])

        assert "sancoes.csv" in ListasDeArquivo(tmp_path).procedencia

    def test_campos_opcionais_vazios_viram_none(self, tmp_path: Path) -> None:
        escrever(tmp_path, "a.csv", ["JOSE SILVA,pep,fonte,,,"])

        entrada = ListasDeArquivo(tmp_path).todas()[0]

        assert entrada.cpf is None
        assert entrada.cargo is None

    def test_linha_sem_nome_e_ignorada(self, tmp_path: Path) -> None:
        escrever(tmp_path, "a.csv", ["JOSE SILVA,pep,fonte,,,", ",pep,fonte,,,"])

        assert ListasDeArquivo(tmp_path).total == 1


class TestFalhaAlto:
    """As tres formas de o servico se recusar a subir.

    Todas existem pelo mesmo motivo: servico de conformidade com lista incompleta
    aprova quem deveria barrar, e reporta "nenhuma correspondencia" ao fazer isso.
    Degradacao silenciosa na direcao mais perigosa possivel.
    """

    def test_diretorio_inexistente(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError, match="nao encontrado"):
            ListasDeArquivo(tmp_path / "nao-existe")

    def test_diretorio_sem_entrada(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError, match="Nenhuma entrada"):
            ListasDeArquivo(tmp_path)

    def test_tipo_invalido_derruba_o_carregamento(self, tmp_path: Path) -> None:
        """Pular a linha em silencio deixaria um sancionado fora da triagem.

        Um erro de digitacao no arquivo nao pode virar uma pessoa nao verificada.
        """
        escrever(tmp_path, "a.csv", ["JOSE SILVA,sancaoo,fonte,,,"])

        with pytest.raises(RuntimeError, match="invalido"):
            ListasDeArquivo(tmp_path)


class TestIsolamento:
    def test_alterar_a_lista_devolvida_nao_afeta_a_fonte(self) -> None:
        from kyc_compliance.domain.triagem import EntradaRestritiva

        original = [EntradaRestritiva(nome="JOSE SILVA", tipo=TipoLista.PEP, origem="teste")]
        listas = ListasEmMemoria(original)

        devolvida = listas.todas()
        devolvida.clear()

        assert listas.total == 1
