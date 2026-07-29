"""Testes dos value objects.

Cobertura focada nas invariantes: o que o tipo promete e o que ele recusa.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from credit_analysis.domain.exceptions import MoedasIncompativeis, ValorInvalido
from credit_analysis.domain.value_objects import CPF, Dinheiro, Percentual


class TestCPF:
    @pytest.mark.parametrize(
        "entrada",
        ["52998224725", "529.982.247-25", "529 982 247 25", "  52998224725  "],
    )
    def test_aceita_formatacoes_equivalentes(self, entrada: str) -> None:
        assert CPF(entrada).numero == "52998224725"

    def test_dois_cpfs_iguais_sao_o_mesmo_valor(self) -> None:
        # Value object nao tem identidade: igualdade e por conteudo.
        assert CPF("529.982.247-25") == CPF("52998224725")

    @pytest.mark.parametrize(
        ("entrada", "motivo"),
        [
            ("529982247", "digitos de menos"),
            ("529982247256", "digitos de mais"),
            ("52998224726", "DV incorreto"),
            ("11111111111", "sequencia repetida"),
            ("00000000000", "sequencia de zeros"),
            ("", "vazio"),
        ],
    )
    def test_rejeita_invalidos(self, entrada: str, motivo: str) -> None:
        with pytest.raises(ValorInvalido):
            CPF(entrada)

    def test_mascara_nao_expoe_digitos_sensiveis(self) -> None:
        mascarado = CPF("52998224725").mascarado
        assert mascarado == "***.982.247-**"
        # O prefixo e o DV — as partes mais identificadoras — ficam ocultos.
        assert "529" not in mascarado
        assert not mascarado.endswith("25")

    def test_str_devolve_formato_com_pontuacao(self) -> None:
        assert str(CPF("52998224725")) == "529.982.247-25"

    def test_e_imutavel(self) -> None:
        cpf = CPF("52998224725")
        with pytest.raises(AttributeError):
            cpf.numero = "11144477735"  # type: ignore[misc]


class TestDinheiro:
    def test_arredonda_para_centavos(self) -> None:
        assert Dinheiro(Decimal("10.005")).valor == Decimal("10.01")
        assert Dinheiro(Decimal("10.004")).valor == Decimal("10.00")

    def test_soma_e_subtracao(self) -> None:
        assert (Dinheiro.de("100.50") + Dinheiro.de("50.25")).valor == Decimal("150.75")
        assert (Dinheiro.de("100.00") - Dinheiro.de("30.50")).valor == Decimal("69.50")

    def test_multiplicacao_por_escalar(self) -> None:
        assert (Dinheiro.de("100.00") * 3).valor == Decimal("300.00")

    def test_operar_moedas_diferentes_falha(self) -> None:
        with pytest.raises(MoedasIncompativeis):
            Dinheiro(Decimal("10"), "BRL") + Dinheiro(Decimal("10"), "USD")

    def test_razao_devolve_percentual(self) -> None:
        parcela = Dinheiro.de("1500.00")
        renda = Dinheiro.de("5000.00")
        assert parcela.razao(renda) == Percentual.de("30.00")

    def test_razao_por_zero_e_comprometimento_total(self) -> None:
        # Renda declarada zerada e caso de negocio real, nao erro de programa.
        assert Dinheiro.de("100").razao(Dinheiro.zero()) == Percentual.de(100)

    def test_nao_acumula_erro_de_ponto_flutuante(self) -> None:
        # 0.1 + 0.2 != 0.3 em float. Com Decimal, e exatamente 0.30.
        total = Dinheiro.de("0.10") + Dinheiro.de("0.20")
        assert total.valor == Decimal("0.30")

    def test_formatacao_brasileira(self) -> None:
        assert str(Dinheiro.de("1234567.89")) == "R$ 1.234.567,89"

    def test_ordenacao(self) -> None:
        valores = [Dinheiro.de("300"), Dinheiro.de("100"), Dinheiro.de("200")]
        assert sorted(valores)[0] == Dinheiro.de("100")


class TestPercentual:
    def test_fracao_converte_para_escala_unitaria(self) -> None:
        assert Percentual.de(30).fracao == Decimal("0.3")

    def test_rejeita_negativo(self) -> None:
        with pytest.raises(ValorInvalido):
            Percentual(Decimal("-1"))

    def test_comparacao(self) -> None:
        assert Percentual.de(30) < Percentual.de(50)
        assert Percentual.de("30.00") == Percentual.de(30)

    def test_formatacao_com_virgula(self) -> None:
        assert str(Percentual.de("29.5")) == "29,50%"
