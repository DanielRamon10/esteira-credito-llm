"""Testes da decisao de triagem.

O foco e a **consequencia por tipo de lista**, que e onde este dominio erra caro:
tratar PEP como impedimento nega credito a servidor publico sem base legal, e
tratar sancao como aviso expoe a instituicao a penalidade.
"""

from __future__ import annotations

import pytest

from kyc_compliance.domain.matching import NivelCorrespondencia
from kyc_compliance.domain.triagem import (
    DecisaoKYC,
    EntradaRestritiva,
    NivelRiscoKYC,
    avaliar,
    classificar,
)
from tests.conftest import CPF_DA_PEP, CPF_DO_SANCIONADO, CPF_LIMPO


class TestDecisaoPorTipoDeLista:
    def test_sem_correspondencia_aprova(self, entradas: list[EntradaRestritiva]) -> None:
        t = avaliar("Beatriz Nogueira Prado", CPF_LIMPO, entradas)

        assert t.decisao is DecisaoKYC.APROVADO
        assert t.nivel_risco is NivelRiscoKYC.BAIXO
        assert t.correspondencias == ()
        assert t.aprovado

    def test_pep_aprova_com_diligencia_e_nao_reprova(
        self, entradas: list[EntradaRestritiva]
    ) -> None:
        """PEP nao e impedimento — e o erro mais comum nesta area.

        A Circular BCB 3.978 art. 27 pede diligencia reforcada e alcada superior.
        Reprovar PEP negaria credito a milhares de servidores publicos sem base
        legal.
        """
        t = avaliar("Maria Fernanda Souza", CPF_DA_PEP, entradas)

        assert t.decisao is DecisaoKYC.APROVADO_COM_DILIGENCIA
        assert t.aprovado, "diligencia reforcada nao e recusa"
        assert any("Nao e impedimento" in j for j in t.justificativas)

    def test_sancao_forte_reprova(self, entradas: list[EntradaRestritiva]) -> None:
        t = avaliar("Jose Antonio Pereira", CPF_LIMPO, entradas)

        assert t.decisao is DecisaoKYC.REPROVADO
        assert t.nivel_risco is NivelRiscoKYC.INACEITAVEL
        assert not t.aprovado

    def test_sancao_parcial_vai_para_humano(self, entradas: list[EntradaRestritiva]) -> None:
        # Nao se reprova por semelhanca de nome, e nao se aprova ignorando o sinal.
        t = avaliar("Marcos Vinicius", CPF_LIMPO, entradas)

        assert t.decisao is DecisaoKYC.REVISAO_MANUAL
        assert t.nivel_risco is NivelRiscoKYC.ALTO

    def test_midia_negativa_vai_para_humano(self, entradas: list[EntradaRestritiva]) -> None:
        t = avaliar("Patricia Gomes de Oliveira", CPF_LIMPO, entradas)

        assert t.decisao is DecisaoKYC.REVISAO_MANUAL


class TestPessoaEmDuasListas:
    def test_sancao_prevalece_sobre_pep(self, entradas: list[EntradaRestritiva]) -> None:
        """O caso que um indice por nome colapsaria.

        "JOSE DA SILVA JR." consta como PEP e em sancao. Se a implementacao
        mapeasse entrada por nome num dicionario, uma das duas desapareceria e a
        decisao dependeria da ordem do arquivo.
        """
        t = avaliar("Jose da Silva Junior", CPF_LIMPO, entradas)

        assert t.decisao is DecisaoKYC.REPROVADO, "a consequencia mais severa manda"
        assert len(t.correspondencias) == 2, "as duas entradas devem aparecer na trilha"


class TestCpfComoIdentificador:
    def test_cpf_e_nome_compativeis_dao_correspondencia_exata(self) -> None:
        assert classificar(score=0.95, cpf_confere=True) is NivelCorrespondencia.EXATA

    def test_cpf_isolado_nao_gera_veto_automatico(self, entradas: list[EntradaRestritiva]) -> None:
        """Defeito real, encontrado exercitando o servico.

        Um cliente com CPF igual ao de um sancionado, mas nome sem nenhuma palavra
        em comum, era reprovado automaticamente com risco inaceitavel. A explicacao
        mais provavel nesse caso nao e mudanca de nome — e erro de digitacao no
        cadastro, e um digito errado num arquivo publico nao pode negar credito
        sem revisao humana.
        """
        t = avaliar("Ana Beatriz Cardoso", CPF_DO_SANCIONADO, entradas)

        # Revisao manual, e nao reprovado: o `is` acima ja garante os dois, porque
        # a decisao e um unico valor. O que importa registrar e que o caminho de
        # veto automatico NAO foi tomado.
        assert t.decisao is DecisaoKYC.REVISAO_MANUAL
        # O sinal do CPF nao se perde: ele fica visivel para o analista.
        assert t.correspondencias[0].cpf_confere
        assert "CPF identico" in t.correspondencias[0].justificativa

    def test_classificacao_por_cpf_isolado_e_parcial(self) -> None:
        assert classificar(score=0.0, cpf_confere=True) is NivelCorrespondencia.PARCIAL


class TestTrilhaDeAuditoria:
    def test_registra_quantas_entradas_foram_avaliadas(
        self, entradas: list[EntradaRestritiva]
    ) -> None:
        # Sem isto, nao ha como provar depois que a triagem cobriu a lista inteira.
        t = avaliar("Qualquer Nome Aqui", CPF_LIMPO, entradas)

        assert t.entradas_avaliadas == len(entradas)

    def test_correspondencias_ordenadas_da_mais_forte(
        self, entradas: list[EntradaRestritiva]
    ) -> None:
        t = avaliar("Jose da Silva Junior", CPF_LIMPO, entradas)

        scores = [c.score for c in t.correspondencias]
        assert scores == sorted(scores, reverse=True)

    def test_justificativa_cita_os_tokens_que_casaram(
        self, entradas: list[EntradaRestritiva]
    ) -> None:
        # Explicabilidade e requisito regulatorio, nao conforto: o analista precisa
        # ver por que o sistema acusou.
        t = avaliar("Carlos Eduardo Lima", CPF_LIMPO, entradas)

        assert "CARLOS" in t.correspondencias[0].justificativa

    def test_cpf_sai_mascarado(self, entradas: list[EntradaRestritiva]) -> None:
        t = avaliar("Beatriz Prado", CPF_LIMPO, entradas)

        assert t.cpf_mascarado == "***.533.447-**"
        assert CPF_LIMPO not in t.cpf_mascarado

    @pytest.mark.parametrize("invalido", ["", "123", "abc"])
    def test_cpf_invalido_nao_quebra_a_mascara(
        self, invalido: str, entradas: list[EntradaRestritiva]
    ) -> None:
        # A validacao vive na borda HTTP; o dominio nao pode estourar por isso.
        t = avaliar("Beatriz Prado", invalido, entradas)
        assert t.cpf_mascarado == "***"
