"""Testes do motor de score.

O foco esta no comportamento observavel de negocio ("restricao cadastral nega
independentemente do score"), nao nos numeros internos. Testar a pontuacao
exata de cada fator amarraria os testes a formula e cada recalibragem do time
de risco quebraria a suite sem que nada estivesse errado.
"""

from __future__ import annotations

import pytest

from credit_analysis.domain import scoring
from credit_analysis.domain.enums import Decisao, NivelRisco
from credit_analysis.domain.value_objects import Dinheiro, Percentual
from tests.conftest import fazer_proposta, fazer_solicitante


def entrada(**kwargs: object) -> scoring.EntradaScore:
    base: dict[str, object] = {
        "solicitante": fazer_solicitante(),
        "proposta": fazer_proposta(),
        "renda_comprovada": None,
        "meses_historico_bancario": 0,
        "tem_restricao_cadastral": False,
    }
    base.update(kwargs)
    return scoring.EntradaScore(**base)  # type: ignore[arg-type]


class TestDecisao:
    def test_restricao_cadastral_nega_mesmo_com_score_alto(self) -> None:
        # Veto duro: nenhuma combinacao de fatores compensa restricao ativa.
        perfil_bom = entrada(
            solicitante=fazer_solicitante(renda="30000.00"),
            proposta=fazer_proposta(valor="10000.00"),
            renda_comprovada=Dinheiro.de("30000.00"),
            meses_historico_bancario=48,
            tem_restricao_cadastral=True,
        )
        assert scoring.avaliar(perfil_bom).decisao is Decisao.NEGADO

    def test_comprometimento_acima_do_teto_nega(self) -> None:
        # Parcela consumindo mais da metade da renda: nao cabe no orcamento.
        apertado = entrada(
            solicitante=fazer_solicitante(renda="3000.00"),
            proposta=fazer_proposta(valor="100000.00", prazo=24),
            renda_comprovada=Dinheiro.de("3000.00"),
            meses_historico_bancario=48,
        )
        parecer = scoring.avaliar(apertado)
        assert parecer.comprometimento_renda > scoring.COMPROMETIMENTO_LIMITE
        assert parecer.decisao is Decisao.NEGADO

    def test_perfil_solido_e_aprovado(self) -> None:
        solido = entrada(
            solicitante=fazer_solicitante(renda="20000.00", idade=38),
            proposta=fazer_proposta(valor="30000.00", prazo=48),
            renda_comprovada=Dinheiro.de("20000.00"),
            meses_historico_bancario=36,
        )
        parecer = scoring.avaliar(solido)
        assert parecer.decisao is Decisao.APROVADO
        assert parecer.nivel_risco is NivelRisco.BAIXO

    def test_caso_limitrofe_vai_para_analise_manual(self) -> None:
        # Score na faixa intermediaria: a esteira nao decide sozinha.
        limitrofe = entrada(
            solicitante=fazer_solicitante(renda="6000.00"),
            proposta=fazer_proposta(valor="40000.00", prazo=48),
            renda_comprovada=None,  # sem comprovacao documental
            meses_historico_bancario=4,
        )
        parecer = scoring.avaliar(limitrofe)
        assert parecer.decisao in {Decisao.ANALISE_MANUAL, Decisao.NEGADO}

    def test_aprovacao_com_ressalva_quando_aperta_mas_cabe(self) -> None:
        parecer = scoring.decidir(
            score=800,
            comprometimento=Percentual.de(42),  # entre confortavel e limite
            tem_restricao=False,
        )
        assert parecer is Decisao.APROVADO_COM_RESSALVAS


class TestFatores:
    def test_renda_comprovada_divergente_derruba_o_score(self) -> None:
        # Mesmo perfil, unica diferenca: a renda comprovada bate ou nao.
        confere = entrada(
            solicitante=fazer_solicitante(renda="10000.00"),
            renda_comprovada=Dinheiro.de("10000.00"),
            meses_historico_bancario=24,
        )
        diverge = entrada(
            solicitante=fazer_solicitante(renda="10000.00"),
            renda_comprovada=Dinheiro.de("4000.00"),
            meses_historico_bancario=24,
        )
        assert scoring.avaliar(confere).score > scoring.avaliar(diverge).score

    def test_historico_mais_longo_pontua_mais(self) -> None:
        curto = entrada(meses_historico_bancario=2)
        longo = entrada(meses_historico_bancario=36)
        assert scoring.avaliar(longo).score > scoring.avaliar(curto).score

    def test_pesos_somam_um(self) -> None:
        # Se alguem adicionar um fator sem rebalancear, este teste avisa.
        fatores, _ = scoring.calcular_fatores(entrada())
        assert sum(f.peso for f in fatores) == pytest.approx(1.0)

    def test_todo_fator_produz_justificativa(self) -> None:
        # Explicabilidade e requisito regulatorio, nao opcional.
        fatores, _ = scoring.calcular_fatores(entrada())
        assert all(f.justificativa.strip() for f in fatores)

    def test_parecer_carrega_todas_as_justificativas(self) -> None:
        parecer = scoring.avaliar(entrada())
        assert len(parecer.justificativas) == 5


class TestClassificacaoRisco:
    @pytest.mark.parametrize(
        ("score", "esperado"),
        [
            (1000, NivelRisco.BAIXO),
            (700, NivelRisco.BAIXO),
            (699, NivelRisco.MEDIO),
            (500, NivelRisco.MEDIO),
            (499, NivelRisco.ALTO),
            (350, NivelRisco.ALTO),
            (349, NivelRisco.CRITICO),
            (0, NivelRisco.CRITICO),
        ],
    )
    def test_faixas_nao_tem_buraco_nem_sobreposicao(self, score: int, esperado: NivelRisco) -> None:
        assert scoring.classificar_risco(score) is esperado


class TestLimiteRecomendado:
    def test_limite_mantem_parcela_na_faixa_confortavel(self) -> None:
        dados = entrada(
            solicitante=fazer_solicitante(renda="10000.00"),
            renda_comprovada=Dinheiro.de("10000.00"),
            meses_historico_bancario=36,
        )
        parecer = scoring.avaliar(dados)
        assert parecer.limite_recomendado is not None

        # Refaz a proposta com o limite sugerido e confere o comprometimento.
        nova = fazer_proposta(valor=str(parecer.limite_recomendado.valor), prazo=36, taxa="1.99")
        assert nova.parcela_mensal.razao(Dinheiro.de("10000.00")) <= (
            scoring.COMPROMETIMENTO_CONFORTAVEL
        )

    def test_score_maior_libera_limite_maior(self) -> None:
        fraco = entrada(meses_historico_bancario=0, tem_restricao_cadastral=True)
        forte = entrada(
            renda_comprovada=fazer_solicitante().renda_mensal_declarada,
            meses_historico_bancario=36,
        )
        limite_fraco = scoring.avaliar(fraco).limite_recomendado
        limite_forte = scoring.avaliar(forte).limite_recomendado
        assert limite_fraco is not None and limite_forte is not None
        assert limite_forte.valor > limite_fraco.valor
