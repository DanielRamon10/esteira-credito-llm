"""Testes da analise de extrato bancario com Pandas."""

from __future__ import annotations

from datetime import date

import pytest

from credit_analysis.domain.exceptions import DadosInsuficientes
from credit_analysis.domain.extrato import (
    CV_RENDA_INSTAVEL,
    MESES_MINIMOS,
    Transacao,
    analisar_extrato,
)
from credit_analysis.domain.value_objects import Dinheiro


def salario(mes: int, valor: str = "8000.00", dia: int = 5) -> Transacao:
    return Transacao(
        data=date(2025, mes, dia),
        descricao=f"CREDITO SALARIO EMPRESA XYZ REF {mes:02d}",
        valor=Dinheiro.de(valor),
    )


def despesa(mes: int, valor: str, descricao: str = "PAGAMENTO CARTAO", dia: int = 10) -> Transacao:
    return Transacao(
        data=date(2025, mes, dia),
        descricao=descricao,
        valor=Dinheiro.de(f"-{valor}"),
    )


def extrato_estavel(
    meses: int = 6, renda: str = "8000.00", gasto: str = "5000.00"
) -> list[Transacao]:
    transacoes: list[Transacao] = []
    for mes in range(1, meses + 1):
        transacoes.append(salario(mes, renda))
        transacoes.append(despesa(mes, gasto))
    return transacoes


class TestValidacaoDeEntrada:
    def test_extrato_vazio_falha(self) -> None:
        with pytest.raises(DadosInsuficientes, match="vazio"):
            analisar_extrato([])

    def test_janela_curta_falha_explicitamente(self) -> None:
        # Preferimos falhar a devolver a media de dois meses como se fosse
        # renda comprovada.
        curto = extrato_estavel(meses=MESES_MINIMOS - 1)
        with pytest.raises(DadosInsuficientes, match="minimo"):
            analisar_extrato(curto)

    def test_janela_minima_passa(self) -> None:
        resumo = analisar_extrato(extrato_estavel(meses=MESES_MINIMOS))
        assert resumo.meses_analisados == MESES_MINIMOS


class TestIndicadores:
    def test_renda_media_e_a_media_das_entradas_mensais(self) -> None:
        resumo = analisar_extrato(extrato_estavel(meses=6, renda="8000.00"))
        assert resumo.renda_media_mensal == Dinheiro.de("8000.00")

    def test_despesa_media_usa_valor_absoluto(self) -> None:
        resumo = analisar_extrato(extrato_estavel(meses=6, gasto="5000.00"))
        assert resumo.despesa_media_mensal == Dinheiro.de("5000.00")

    def test_capacidade_de_poupanca_e_a_sobra(self) -> None:
        resumo = analisar_extrato(extrato_estavel(meses=6, renda="8000.00", gasto="5000.00"))
        assert resumo.capacidade_poupanca == Dinheiro.de("3000.00")

    def test_periodo_cobre_primeira_e_ultima_transacao(self) -> None:
        resumo = analisar_extrato(extrato_estavel(meses=6))
        assert resumo.periodo_inicio == date(2025, 1, 5)
        assert resumo.periodo_fim == date(2025, 6, 10)

    def test_conta_meses_com_saldo_negativo(self) -> None:
        transacoes = extrato_estavel(meses=4, renda="5000.00", gasto="3000.00")
        transacoes.append(despesa(2, "9000.00", "SAQUE EMERGENCIA", dia=20))
        resumo = analisar_extrato(transacoes)
        assert resumo.meses_com_saldo_negativo == 1


class TestEstabilidadeDeRenda:
    def test_salario_fixo_e_considerado_estavel(self) -> None:
        resumo = analisar_extrato(extrato_estavel(meses=6, renda="8000.00"))
        assert resumo.renda_estavel
        assert resumo.volatilidade_renda.valor == 0

    def test_renda_muito_variavel_e_instavel(self) -> None:
        # Perfil de autonomo: entradas oscilando de 1k a 20k.
        valores = ["1000.00", "20000.00", "3000.00", "18000.00", "2000.00", "15000.00"]
        transacoes = [salario(mes, v) for mes, v in enumerate(valores, start=1)]
        resumo = analisar_extrato(transacoes)

        assert not resumo.renda_estavel
        assert resumo.volatilidade_renda.fracao > CV_RENDA_INSTAVEL

    def test_mediana_resiste_a_mes_atipico(self) -> None:
        # Um 13o salario nao deve inflar a renda "tipica".
        transacoes = extrato_estavel(meses=6, renda="8000.00")
        transacoes.append(salario(6, "50000.00", dia=20))
        resumo = analisar_extrato(transacoes)

        assert resumo.renda_mediana_mensal < resumo.renda_media_mensal


class TestRecorrencia:
    def test_detecta_salario_como_credito_recorrente(self) -> None:
        resumo = analisar_extrato(extrato_estavel(meses=6))
        assert any("SALARIO" in c for c in resumo.creditos_recorrentes)

    def test_ignora_credito_pontual(self) -> None:
        transacoes = extrato_estavel(meses=6)
        transacoes.append(
            Transacao(
                data=date(2025, 3, 15),
                descricao="TED RECEBIDA VENDA CARRO",
                valor=Dinheiro.de("30000.00"),
            )
        )
        resumo = analisar_extrato(transacoes)
        assert not any("VENDA CARRO" in c for c in resumo.creditos_recorrentes)

    def test_agrupa_apesar_do_numero_de_documento_variavel(self) -> None:
        # As descricoes diferem apenas no sufixo numerico; devem virar uma
        # unica entrada recorrente, nao seis distintas.
        resumo = analisar_extrato(extrato_estavel(meses=6))
        salarios = [c for c in resumo.creditos_recorrentes if "SALARIO" in c]
        assert len(salarios) == 1
