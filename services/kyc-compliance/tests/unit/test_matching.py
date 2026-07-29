"""Testes do casamento de nomes.

Cada teste aqui corresponde a uma forma real de o mesmo nome ser escrito de dois
jeitos, ou a um par de nomes distintos que um algoritmo ingenuo confundiria. Os
comentarios registram qual dos dois.
"""

from __future__ import annotations

from kyc_compliance.domain.matching import (
    comparar,
    normalizar,
    tokenizar,
)
from kyc_compliance.domain.triagem import LIMIAR_FORTE, LIMIAR_PARCIAL


class TestNormalizacao:
    def test_remove_acento_e_caixa(self) -> None:
        assert normalizar("José Antônio Pereira") == "JOSE ANTONIO PEREIRA"

    def test_remove_pontuacao_e_colapsa_espaco(self) -> None:
        assert normalizar("  JOSE  DA SILVA JR.  ") == "JOSE DA SILVA JR"

    def test_descarta_particula_mas_nao_a_letra_e(self) -> None:
        """`E` fica porque pode ser inicial do nome do meio.

        Descartar como conjuncao anulava a regra de inicial abreviada e derrubava
        um casamento legitimo de 0,93 para 0,69 — abaixo de um par que NAO deveria
        casar. A medicao esta no cabecalho de `matching.py`.
        """
        assert tokenizar("Jose da Silva") == ("JOSE", "SILVA")
        assert tokenizar("CARLOS E. LIMA") == ("CARLOS", "E", "LIMA")

    def test_expande_sufixo_geracional(self) -> None:
        assert tokenizar("Jose da Silva Jr.") == ("JOSE", "SILVA", "JUNIOR")


class TestMesmaPessoa:
    """Pares que DEVEM casar acima do limiar forte."""

    def test_abreviacao_geracional(self) -> None:
        score, _, _ = comparar("Jose da Silva Junior", "JOSE DA SILVA JR.")
        assert score >= LIMIAR_FORTE

    def test_acento(self) -> None:
        score, _, _ = comparar("José Antônio Pereira", "JOSE ANTONIO PEREIRA")
        assert score >= LIMIAR_FORTE

    def test_inicial_do_meio_abreviada_na_lista(self) -> None:
        score, _, ausentes = comparar("Carlos Eduardo Lima", "CARLOS E. LIMA")
        assert score >= LIMIAR_FORTE
        assert not ausentes

    def test_erro_de_digitacao_em_token_longo(self) -> None:
        # Remocao de uma letra em token de 9 caracteres: digitacao, nao outra pessoa.
        score, _, _ = comparar("Ana Paula Rodrigues", "ANA PAULA RODRIGES")
        assert score >= LIMIAR_FORTE

    def test_transposicao_de_letras(self) -> None:
        # Assinatura classica de digitacao, aceita em qualquer tamanho de token.
        score, _, _ = comparar("Maria Silva", "MARIA SLIVA")
        assert score >= LIMIAR_FORTE

    def test_particula_ausente_de_um_lado(self) -> None:
        score, _, _ = comparar("Roberto Carlos de Almeida", "ROBERTO CARLOS ALMEIDA")
        assert score >= LIMIAR_FORTE


class TestPessoasDiferentes:
    """Pares que NAO devem casar como forte."""

    def test_troca_de_genero_na_ultima_letra(self) -> None:
        """O falso positivo que a medicao pegou.

        Antes de separar substituicao de transposicao, este par pontuava 0,968 —
        nivel praticamente identico, o pior lugar possivel para um falso positivo,
        porque o analista confia e aprova.
        """
        score, _, ausentes = comparar("Maria Silva", "Mario Silva")
        assert score < LIMIAR_PARCIAL
        assert "MARIA" in ausentes

    def test_nome_de_batismo_diferente(self) -> None:
        score, _, _ = comparar("Pedro Henrique Alves", "Paulo Henrique Alves")
        assert score < LIMIAR_FORTE

    def test_sobrenome_estrangeiro_parecido(self) -> None:
        score, _, _ = comparar("Lucas Martins", "Lucas Martinez")
        assert score < LIMIAR_FORTE

    def test_token_curto_nao_tolera_edicao(self) -> None:
        # "ANA" e "ANO" ficam a um caractere; aceitar isso confundiria nomes curtos.
        score, _, _ = comparar("Ana Costa", "Ano Costa")
        assert score < LIMIAR_FORTE

    def test_nome_consultado_e_subconjunto_do_da_lista(self) -> None:
        """Fica em revisao, nao em veto — e nem aprovado direto.

        "Jose da Silva" contra "Jose da Silva Rodrigues" e genuinamente ambiguo:
        pode ser a mesma pessoa com sobrenome omitido, pode ser outra. O lugar
        certo dele e a faixa parcial, que leva a humano.
        """
        score, _, _ = comparar("Jose da Silva", "Jose da Silva Rodrigues")
        assert LIMIAR_PARCIAL <= score < LIMIAR_FORTE


class TestRobustez:
    def test_nome_vazio_nao_casa_com_nada(self) -> None:
        assert comparar("", "JOSE DA SILVA")[0] == 0.0
        assert comparar("JOSE DA SILVA", "")[0] == 0.0

    def test_so_particula_nao_casa(self) -> None:
        # "DA DE DOS" tokeniza para nada; sem guarda isso dividiria por zero.
        assert comparar("da de dos", "JOSE DA SILVA")[0] == 0.0

    def test_token_repetido_nao_infla_cobertura(self) -> None:
        """Cada token da lista e consumido uma unica vez.

        Sem isso, "SILVA SILVA" casaria duas vezes contra um unico "SILVA" e a
        cobertura passaria de 100%.
        """
        score, casados, _ = comparar("Silva Silva", "SILVA")
        assert len(casados) == 1
        assert score < LIMIAR_FORTE

    def test_score_sempre_entre_zero_e_um(self) -> None:
        pares = [
            ("Jose", "JOSE"),
            ("A", "B"),
            ("Jose da Silva", "Jose da Silva Rodrigues Pereira Almeida"),
        ]
        for a, b in pares:
            score, _, _ = comparar(a, b)
            assert 0.0 <= score <= 1.0
