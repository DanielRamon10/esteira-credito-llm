"""Testes da fronteira de divulgacao — o guard de saida.

Este e o problema que os outros dois servicos nao tem: eles respondem a analista,
este responde ao cliente. Revelar limiar interno nao vaza dado pessoal; entrega o
mapa para burlar a esteira.
"""

from __future__ import annotations

import pytest

from customer_support.domain.divulgacao import descrever_padroes, inspecionar


class TestBloqueia:
    @pytest.mark.parametrize(
        ("texto", "categoria"),
        [
            ("Conforme a POL-001, isso nao e permitido.", "referencia_politica_interna"),
            (
                "Sua proposta foi negada porque o score ficou abaixo de 700 pontos.",
                "limiar_de_score",
            ),
            ("Score minimo para aprovacao automatica: 700.", "limiar_de_score"),
            ("Sao necessarios 700 pontos para aprovacao direta.", "limiar_de_score"),
            ("Nesse valor depende da alcada do gerente regional.", "alcada_de_aprovacao"),
            ("O comprometimento de renda tem peso de 40% no score.", "peso_de_fator"),
            ("Comprometimento acima de 50% e vedado por politica.", "teto_de_comprometimento"),
            ("Os dados ficam no pgvector da esteira interna.", "sistema_interno"),
        ],
    )
    def test_categoria_certa(self, texto: str, categoria: str) -> None:
        veredito = inspecionar(texto)

        assert veredito.bloqueada
        assert categoria in veredito.vazamentos

    def test_acento_nao_contorna(self) -> None:
        """A inspecao roda sobre texto normalizado.

        Um padrao que so casasse a forma sem acento seria contornado por qualquer
        variacao de escrita do modelo — e o modelo varia.
        """
        assert inspecionar("Depende da alçada do gerente.").bloqueada


class TestLibera:
    @pytest.mark.parametrize(
        "texto",
        [
            "Para comprovar renda, envie os tres ultimos holerites.",
            "A taxa do consignado varia conforme o convenio.",
            "Seu score e um indicador usado por diversas instituicoes financeiras.",
            "Comprometimento de renda e quanto da sua renda a parcela consome.",
            "Voce pode consultar seu score gratuitamente nos birôs de credito.",
            "Nao ha tarifa para antecipar parcelas.",
        ],
    )
    def test_conteudo_publico_passa(self, texto: str) -> None:
        """Falso positivo aqui nao e barato: descarta a prosa e cai no artigo cru."""
        veredito = inspecionar(texto)

        assert veredito.liberada, f"bloqueou indevidamente: {veredito.vazamentos}"


class TestContratoDoGuard:
    def test_todos_os_padroes_tem_nome(self) -> None:
        # Nome vai para log e metrica: "vazamento detectado" sem categoria nao permite
        # corrigir o prompt nem o corpus.
        nomes = descrever_padroes()

        assert len(nomes) == len(set(nomes))
        assert all(nome and " " not in nome for nome in nomes)

    def test_texto_vazio_e_liberado(self) -> None:
        assert inspecionar("").liberada
