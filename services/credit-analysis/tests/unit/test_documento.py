"""Testes dos conceitos de dominio da extracao documental."""

from __future__ import annotations

from decimal import Decimal

import pytest

from credit_analysis.domain.documento import (
    CONFIANCA_MINIMA_ACEITAVEL,
    CONFIANCA_MINIMA_AUTOMATICA,
    CampoExtraido,
    ExtracaoHolerite,
    QualidadeExtracao,
    ResultadoOCR,
    classificar_qualidade,
    parsear_dinheiro_brl,
    parsear_valor_brl,
)
from credit_analysis.domain.value_objects import Dinheiro, Percentual


class TestParseValorBRL:
    @pytest.mark.parametrize(
        ("entrada", "esperado"),
        [
            ("8.500,00", "8500.00"),
            ("420,50", "420.50"),
            ("1.234.567,89", "1234567.89"),
            ("0,01", "0.01"),
            ("R$ 1.234,56", "1234.56"),
            # OCR confunde $ com S: "RS" em vez de "R$".
            ("RS 900,00", "900.00"),
            ("  8.500,00  ", "8500.00"),
        ],
    )
    def test_formatos_validos(self, entrada: str, esperado: str) -> None:
        assert parsear_valor_brl(entrada) == Decimal(esperado)

    def test_ponto_como_separador_de_milhar_nao_decimal(self) -> None:
        # Ler "8.500,00" como Decimal direto daria 8.5 — erro de tres ordens de
        # grandeza num campo de renda, e sem excecao para avisar.
        assert parsear_valor_brl("8.500,00") == Decimal("8500.00")
        assert parsear_valor_brl("8.500") == Decimal("8500")

    @pytest.mark.parametrize(
        ("entrada", "esperado"),
        [
            ("2.100,00 D", "-2100.00"),  # sufixo de debito
            ("2.100,00 C", "2100.00"),
            ("-500,00", "-500.00"),
        ],
    )
    def test_sinal_e_sufixo(self, entrada: str, esperado: str) -> None:
        assert parsear_valor_brl(entrada) == Decimal(esperado)

    @pytest.mark.parametrize("entrada", ["", "   ", "lixo", "ABC", "---", "R$"])
    def test_entradas_invalidas_devolvem_none(self, entrada: str) -> None:
        # None em vez de excecao: campo ilegivel e caso esperado quando a
        # entrada vem de OCR.
        assert parsear_valor_brl(entrada) is None

    def test_dinheiro_brl(self) -> None:
        assert parsear_dinheiro_brl("8.500,00") == Dinheiro.de("8500.00")
        assert parsear_dinheiro_brl("lixo") is None


class TestClassificacaoQualidade:
    @pytest.mark.parametrize(
        ("confianca", "esperado"),
        [
            (100, QualidadeExtracao.CONFIAVEL),
            (85, QualidadeExtracao.CONFIAVEL),
            (84.99, QualidadeExtracao.REVISAO_HUMANA),
            (60, QualidadeExtracao.REVISAO_HUMANA),
            (59.99, QualidadeExtracao.REJEITADA),
            (0, QualidadeExtracao.REJEITADA),
        ],
    )
    def test_faixas_da_politica(self, confianca: float, esperado: QualidadeExtracao) -> None:
        # Limiares da POL-002 secao 3.2.
        assert classificar_qualidade(Percentual.de(confianca)) is esperado

    def test_limiares_sao_coerentes(self) -> None:
        assert CONFIANCA_MINIMA_ACEITAVEL < CONFIANCA_MINIMA_AUTOMATICA


class TestResultadoOCR:
    def test_aproveitavel_acima_do_piso(self) -> None:
        assert ResultadoOCR("texto", Percentual.de(70), "m").aproveitavel
        assert not ResultadoOCR("texto", Percentual.de(50), "m").aproveitavel

    def test_vazio(self) -> None:
        assert ResultadoOCR("   ", Percentual.de(99), "m").vazio
        assert not ResultadoOCR("algo", Percentual.de(99), "m").vazio


def campo(nome: str, valor: str, confianca: float = 90) -> CampoExtraido:
    return CampoExtraido(
        nome=nome,
        valor_bruto=valor,
        trecho_origem=f"{nome}: {valor}",
        confianca=Percentual.de(confianca),
    )


class TestExtracaoHolerite:
    def test_liquido_tem_precedencia_sobre_base(self) -> None:
        # O liquido e o que entra na conta e, portanto, o que paga parcela.
        # Usar o bruto infla a capacidade de pagamento em ~20%.
        extracao = ExtracaoHolerite(
            salario_base=campo("salario_base", "8.500,00"),
            salario_liquido=campo("salario_liquido", "7.262,14"),
        )
        assert extracao.renda_comprovada == Dinheiro.de("7262.14")

    def test_cai_para_o_base_quando_nao_ha_liquido(self) -> None:
        extracao = ExtracaoHolerite(salario_base=campo("salario_base", "8.500,00"))
        assert extracao.renda_comprovada == Dinheiro.de("8500.00")

    def test_sem_renda_devolve_none(self) -> None:
        assert ExtracaoHolerite().renda_comprovada is None

    def test_valor_ilegivel_nao_conta_como_renda(self) -> None:
        extracao = ExtracaoHolerite(salario_liquido=campo("salario_liquido", "[ILEGIVEL]"))
        assert extracao.renda_comprovada is None

    def test_renda_zero_nao_conta(self) -> None:
        extracao = ExtracaoHolerite(salario_liquido=campo("salario_liquido", "0,00"))
        assert extracao.renda_comprovada is None

    def test_completa_exige_renda_e_identificacao(self) -> None:
        so_renda = ExtracaoHolerite(salario_liquido=campo("salario_liquido", "7.262,14"))
        assert not so_renda.completa

        com_cpf = ExtracaoHolerite(
            salario_liquido=campo("salario_liquido", "7.262,14"),
            cpf=campo("cpf", "52998224725"),
        )
        assert com_cpf.completa

    def test_campo_guarda_o_trecho_de_origem(self) -> None:
        # E o que permite ao analista conferir sem reabrir o documento.
        c = campo("salario_liquido", "7.262,14")
        assert "7.262,14" in c.trecho_origem
