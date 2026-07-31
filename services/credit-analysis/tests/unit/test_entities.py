"""Testes das entidades e da maquina de estados do agregado."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from credit_analysis.domain.armazenamento import EstadoDocumento
from credit_analysis.domain.entities import (
    AnaliseCredito,
    DocumentoSubmetido,
    Parecer,
    PropostaCredito,
    Solicitante,
)
from credit_analysis.domain.enums import Decisao, NivelRisco, StatusAnalise, TipoDocumento
from credit_analysis.domain.exceptions import TransicaoInvalida, ValorInvalido
from credit_analysis.domain.value_objects import CPF, Dinheiro, Percentual
from tests.conftest import CPF_VALIDO, fazer_proposta, fazer_solicitante


class TestSolicitante:
    def test_calcula_idade(self) -> None:
        assert fazer_solicitante(idade=30).idade == 30

    def test_rejeita_menor_de_idade(self) -> None:
        with pytest.raises(ValorInvalido, match="maior de idade"):
            fazer_solicitante(idade=17)

    def test_rejeita_nome_vazio(self) -> None:
        with pytest.raises(ValorInvalido, match="Nome"):
            Solicitante(
                nome="   ",
                cpf=CPF(CPF_VALIDO),
                data_nascimento=datetime(1990, 1, 1, tzinfo=UTC),
                renda_mensal_declarada=Dinheiro.de("5000"),
            )


class TestPropostaCredito:
    def test_parcela_pela_tabela_price(self) -> None:
        # R$ 10.000 em 12x a 1% a.m. -> PMT ~= R$ 888,49 (valor de referencia
        # conferido em calculadora financeira independente).
        proposta = fazer_proposta(valor="10000.00", prazo=12, taxa="1.00")
        assert proposta.parcela_mensal.valor == pytest.approx(
            Decimal("888.49"), abs=Decimal("0.01")
        )

    def test_taxa_zero_e_divisao_simples(self) -> None:
        proposta = fazer_proposta(valor="12000.00", prazo=12, taxa="0")
        assert proposta.parcela_mensal.valor == Decimal("1000.00")

    def test_custo_total_supera_o_principal_com_juros(self) -> None:
        proposta = fazer_proposta(valor="10000.00", prazo=24, taxa="1.50")
        assert proposta.custo_total.valor > Decimal("10000.00")

    @pytest.mark.parametrize("prazo", [0, -1, 121])
    def test_rejeita_prazo_fora_da_politica(self, prazo: int) -> None:
        with pytest.raises(ValorInvalido, match="Prazo"):
            PropostaCredito(
                valor_solicitado=Dinheiro.de("1000"),
                prazo_meses=prazo,
                taxa_juros_mensal=Percentual.de(1),
            )

    def test_rejeita_valor_nao_positivo(self) -> None:
        with pytest.raises(ValorInvalido, match="positivo"):
            PropostaCredito(
                valor_solicitado=Dinheiro.zero(),
                prazo_meses=12,
                taxa_juros_mensal=Percentual.de(1),
            )


def parecer_qualquer() -> Parecer:
    return Parecer(
        decisao=Decisao.APROVADO,
        nivel_risco=NivelRisco.BAIXO,
        score=800,
        comprometimento_renda=Percentual.de(20),
    )


class TestMaquinaDeEstados:
    def test_fluxo_feliz(self) -> None:
        analise = AnaliseCredito(fazer_solicitante(), fazer_proposta())
        assert analise.status is StatusAnalise.PENDENTE

        analise.iniciar_processamento()
        assert analise.status is StatusAnalise.PROCESSANDO

        analise.concluir(parecer_qualquer())
        assert analise.status is StatusAnalise.CONCLUIDA
        assert analise.finalizada

    def test_nao_pula_de_pendente_para_concluida(self) -> None:
        analise = AnaliseCredito(fazer_solicitante(), fazer_proposta())
        with pytest.raises(TransicaoInvalida):
            analise.concluir(parecer_qualquer())

    def test_concluida_so_reabre_pela_porta_da_reavaliacao(self) -> None:
        analise = AnaliseCredito(fazer_solicitante(), fazer_proposta())
        analise.iniciar_processamento()
        analise.concluir(parecer_qualquer())

        # `iniciar_processamento` bypassaria o contador de reaberturas e
        # deixaria a reavaliacao invisivel na auditoria.
        with pytest.raises(TransicaoInvalida, match="reabrir_para_reavaliacao"):
            analise.iniciar_processamento()

        # Falha tambem nao: erro de infraestrutura nao se resolve reabrindo.
        with pytest.raises(TransicaoInvalida):
            analise.falhar("tentativa indevida")

    def test_falha_e_terminal(self) -> None:
        analise = AnaliseCredito(fazer_solicitante(), fazer_proposta())
        analise.iniciar_processamento()
        analise.falhar("bureau fora do ar")

        with pytest.raises(TransicaoInvalida):
            analise.iniciar_processamento()
        with pytest.raises(TransicaoInvalida):
            analise.reabrir_para_reavaliacao("documento novo")

    def test_falha_registra_motivo(self) -> None:
        analise = AnaliseCredito(fazer_solicitante(), fazer_proposta())
        analise.iniciar_processamento()
        analise.falhar("bureau indisponivel")

        assert analise.status is StatusAnalise.FALHA
        assert analise.erro == "bureau indisponivel"
        assert analise.finalizada

    def test_documento_entra_antes_e_durante_o_processamento(self) -> None:
        analise = AnaliseCredito(fazer_solicitante(), fazer_proposta())
        doc = DocumentoSubmetido(
            tipo=TipoDocumento.HOLERITE,
            nome_arquivo="holerite.pdf",
            conteudo_hash="abc123",
        )
        analise.anexar_documento(doc)
        analise.iniciar_processamento()
        analise.anexar_documento(doc)  # comprovacao chega durante a esteira

        assert len(analise.documentos) == 2

    def test_documento_nao_entra_em_analise_concluida_sem_reabrir(self) -> None:
        # Anexar a uma analise fechada deixaria o parecer descolado da evidencia.
        analise = AnaliseCredito(fazer_solicitante(), fazer_proposta())
        analise.iniciar_processamento()
        analise.concluir(parecer_qualquer())

        doc = DocumentoSubmetido(
            tipo=TipoDocumento.HOLERITE, nome_arquivo="h.pdf", conteudo_hash="x"
        )
        with pytest.raises(TransicaoInvalida, match="reabra"):
            analise.anexar_documento(doc)


class TestReavaliacao:
    """Reabertura por apresentacao de documento — o fluxo que a POL-002 exige.

    A esteira emite parecer preliminar com a renda declarada; o cliente
    apresenta comprovacao depois. Sem reabertura, documento nenhum poderia ser
    anexado a uma analise ja avaliada.
    """

    def _concluida(self) -> AnaliseCredito:
        analise = AnaliseCredito(fazer_solicitante(), fazer_proposta())
        analise.iniciar_processamento()
        analise.concluir(parecer_qualquer())
        return analise

    def test_reabre_e_aceita_documento(self) -> None:
        analise = self._concluida()
        analise.reabrir_para_reavaliacao("holerite apresentado")

        assert analise.status is StatusAnalise.PROCESSANDO
        assert analise.reavaliacoes == 1
        assert analise.motivo_reavaliacao == "holerite apresentado"

        analise.anexar_documento(
            DocumentoSubmetido(tipo=TipoDocumento.HOLERITE, nome_arquivo="h.pdf", conteudo_hash="x")
        )
        assert len(analise.documentos) == 1

    def test_novo_parecer_substitui_o_anterior(self) -> None:
        analise = self._concluida()
        assert analise.parecer is not None and analise.parecer.score == 800

        analise.reabrir_para_reavaliacao("renda comprovada")
        analise.concluir(
            Parecer(
                decisao=Decisao.APROVADO_COM_RESSALVAS,
                nivel_risco=NivelRisco.MEDIO,
                score=620,
                comprometimento_renda=Percentual.de(38),
            )
        )

        assert analise.parecer.score == 620
        assert analise.status is StatusAnalise.CONCLUIDA

    def test_limite_de_reaberturas(self) -> None:
        # Sem teto, o cliente reenviaria documento ate obter o parecer que quer.
        from credit_analysis.domain.entities import MAX_REAVALIACOES

        analise = self._concluida()
        for _ in range(MAX_REAVALIACOES):
            analise.reabrir_para_reavaliacao("nova tentativa")
            analise.concluir(parecer_qualquer())

        with pytest.raises(TransicaoInvalida, match="limite"):
            analise.reabrir_para_reavaliacao("uma vez a mais")

    def test_reabertura_limpa_erro_anterior(self) -> None:
        analise = self._concluida()
        analise.reabrir_para_reavaliacao("documento novo")
        assert analise.erro is None

    def test_atualizada_em_avanca_a_cada_transicao(self) -> None:
        analise = AnaliseCredito(fazer_solicitante(), fazer_proposta())
        antes = analise.atualizada_em
        analise.iniciar_processamento()
        assert analise.atualizada_em >= antes


class TestParecer:
    @pytest.mark.parametrize("score", [-1, 1001])
    def test_rejeita_score_fora_da_escala(self, score: int) -> None:
        with pytest.raises(ValorInvalido, match="Score"):
            Parecer(
                decisao=Decisao.APROVADO,
                nivel_risco=NivelRisco.BAIXO,
                score=score,
                comprometimento_renda=Percentual.de(10),
            )


class TestDocumentoSubmetido:
    def test_nao_processado_antes_do_ocr(self) -> None:
        doc = DocumentoSubmetido(
            tipo=TipoDocumento.EXTRATO_BANCARIO,
            nome_arquivo="extrato.pdf",
            conteudo_hash="hash",
        )
        assert not doc.processado
        assert doc.estado is EstadoDocumento.RECEBIDO

        doc.concluir_extracao("SALDO ANTERIOR ...", Percentual.de("95"))

        assert doc.processado
        assert doc.estado is EstadoDocumento.EXTRAIDO

    def test_atribuir_o_texto_direto_nao_marca_como_processado(self) -> None:
        """Mudanca de contrato da Camada 8, e ela e o ponto do estado existir.

        Antes, `processado` era `texto_extraido is not None` — logo atribuir o campo bastava.
        Agora `estado` e a fonte de verdade, porque o booleano colapsava tres situacoes: na
        fila, falhou por erro tecnico, e reprovado no piso de qualidade da POL-002. Com um
        `bool`, o canal de atendimento nao tinha o que dizer a quem enviou o documento.

        O efeito colateral e este: atribuir o texto sozinho **nao** conclui a extracao. E
        desejado — aquele estado (texto presente, ainda `recebido`) nao existe no fluxo, e
        deixa-lo passar em silencio traria de volta o problema de dois campos que divergem.
        """
        doc = DocumentoSubmetido(
            tipo=TipoDocumento.EXTRATO_BANCARIO,
            nome_arquivo="extrato.pdf",
            conteudo_hash="hash",
        )

        doc.texto_extraido = "SALDO ANTERIOR ..."

        assert not doc.processado

    def test_rejeicao_por_qualidade_preserva_o_texto(self) -> None:
        """Descartar o texto pareceria mais limpo e destruiria a evidencia.

        Sem ele nao ha como auditar por que a rejeicao aconteceu, nem comparar com o reenvio.
        """
        doc = DocumentoSubmetido(
            tipo=TipoDocumento.HOLERITE, nome_arquivo="h.png", conteudo_hash="hash"
        )
        doc.texto_extraido = "texto ilegivel"

        doc.rejeitar_por_qualidade("confianca abaixo do piso", Percentual.de("41"))

        assert doc.estado is EstadoDocumento.REJEITADO
        assert doc.texto_extraido == "texto ilegivel"
        assert not doc.processado

    def test_marcar_extraindo_e_idempotente(self) -> None:
        """Entrega de mensagem e *at-least-once*: a mesma extracao pode comecar duas vezes."""
        doc = DocumentoSubmetido(
            tipo=TipoDocumento.HOLERITE, nome_arquivo="h.png", conteudo_hash="hash"
        )

        doc.marcar_extraindo()
        doc.marcar_extraindo()

        assert doc.estado is EstadoDocumento.EXTRAINDO

    def test_marcar_extraindo_nao_desfaz_estado_terminal(self) -> None:
        """Uma mensagem reentregue depois da conclusao nao pode reabrir o documento.

        Sem esta guarda, o reprocessamento de uma mensagem duplicada devolveria um documento
        `extraido` para `extraindo`, e o `GET` diria "processando" sobre algo ja concluido.
        """
        doc = DocumentoSubmetido(
            tipo=TipoDocumento.HOLERITE, nome_arquivo="h.png", conteudo_hash="hash"
        )
        doc.concluir_extracao("texto", Percentual.de("95"))

        doc.marcar_extraindo()

        assert doc.estado is EstadoDocumento.EXTRAIDO
