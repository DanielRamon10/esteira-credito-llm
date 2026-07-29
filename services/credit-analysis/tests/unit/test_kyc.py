"""Testes do gate de conformidade e do cliente que o alimenta.

O que se testa aqui, em ordem de importancia:

1. **O gate so aperta.** Nenhum estado de KYC melhora uma decisao de credito.
2. **Indisponibilidade nao aprova nem nega.** Vai para revisao humana, com o
   motivo dito na justificativa.
3. **O disjuntor abre, protege e volta.** Sem ele, um KYC fora do ar derruba a
   esteira por acumulo de timeout.
4. **A traducao e estrita.** Contrato divergente vira erro visivel, nao um default.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from credit_analysis.domain import scoring
from credit_analysis.domain.entities import Parecer
from credit_analysis.domain.enums import Decisao, NivelRisco
from credit_analysis.domain.kyc import DecisaoKYC, ResultadoKYC
from credit_analysis.domain.value_objects import Percentual
from credit_analysis.infrastructure.kyc import (
    ClienteKYCHttp,
    Disjuntor,
    EstadoDisjuntor,
    KYCFake,
    _ErroPermanente,
    _traduzir,
)


def parecer_aprovado() -> Parecer:
    return Parecer(
        decisao=Decisao.APROVADO,
        nivel_risco=NivelRisco.BAIXO,
        score=780,
        comprometimento_renda=Percentual.de(Decimal("18")),
        justificativas=["Comprometimento de renda em 18,00% — dentro da faixa confortavel"],
    )


class TestGateSoAperta:
    """A propriedade que torna a composicao auditavel."""

    def test_kyc_aprovado_devolve_o_parecer_intocado(self) -> None:
        original = parecer_aprovado()

        resultado = scoring.aplicar_gate_kyc(original, ResultadoKYC(decisao=DecisaoKYC.APROVADO))

        assert resultado.decisao is Decisao.APROVADO
        assert resultado.justificativas == original.justificativas

    @pytest.mark.parametrize(
        "decisao_kyc",
        [
            DecisaoKYC.APROVADO_COM_DILIGENCIA,
            DecisaoKYC.REVISAO_MANUAL,
            DecisaoKYC.REPROVADO,
            DecisaoKYC.INDISPONIVEL,
        ],
    )
    def test_nenhum_estado_melhora_a_decisao(self, decisao_kyc: DecisaoKYC) -> None:
        """Partindo de NEGADO, nada no KYC pode aprovar.

        E a garantia estrutural: o gate e uma restricao, nao uma renegociacao.
        """
        negado = Parecer(
            decisao=Decisao.NEGADO,
            nivel_risco=NivelRisco.CRITICO,
            score=300,
            comprometimento_renda=Percentual.de(Decimal("62")),
        )

        resultado = scoring.aplicar_gate_kyc(negado, ResultadoKYC(decisao=decisao_kyc))

        assert resultado.decisao is Decisao.NEGADO

    def test_nao_muta_o_parecer_recebido(self) -> None:
        # `replace` cria novo objeto. Mutar o original faria a auditoria perder a
        # nota que o motor de score deu antes do gate.
        original = parecer_aprovado()
        quantidade_antes = len(original.justificativas)

        scoring.aplicar_gate_kyc(original, ResultadoKYC(decisao=DecisaoKYC.REPROVADO))

        assert original.decisao is Decisao.APROVADO
        assert len(original.justificativas) == quantidade_antes


class TestVetoESeusLimites:
    def test_sancao_nega_independentemente_do_score(self) -> None:
        resultado = scoring.aplicar_gate_kyc(
            parecer_aprovado(),
            ResultadoKYC(
                decisao=DecisaoKYC.REPROVADO, justificativas=("Correspondencia forte em sancoes",)
            ),
        )

        assert resultado.decisao is Decisao.NEGADO
        assert resultado.nivel_risco is NivelRisco.CRITICO
        # O score original continua no parecer: a auditoria precisa ver que o
        # veto foi de conformidade e nao de capacidade de pagamento.
        assert resultado.score == 780
        assert any("veto independente do score" in j for j in resultado.justificativas)

    def test_pep_nao_nega_mas_impede_aprovacao_automatica(self) -> None:
        """A sutileza regulatoria mais facil de errar.

        O KYC responde "aprovado com diligencia" — nao e recusa. Mas a Circular BCB
        3.978 art. 27 exige alcada superior, e uma aprovacao **automatica** de PEP e
        exatamente o que a regra proibe. Logo: analise manual, nao negado.
        """
        resultado = scoring.aplicar_gate_kyc(
            parecer_aprovado(),
            ResultadoKYC(
                decisao=DecisaoKYC.APROVADO_COM_DILIGENCIA,
                justificativas=("Pessoa Exposta Politicamente",),
            ),
        )

        assert resultado.decisao is Decisao.ANALISE_MANUAL
        assert resultado.decisao is not Decisao.NEGADO
        assert any("alcada superior" in j for j in resultado.justificativas)

    def test_revisao_manual_do_kyc_vira_analise_manual(self) -> None:
        resultado = scoring.aplicar_gate_kyc(
            parecer_aprovado(), ResultadoKYC(decisao=DecisaoKYC.REVISAO_MANUAL)
        )

        assert resultado.decisao is Decisao.ANALISE_MANUAL


class TestIndisponibilidade:
    """O estado que um cliente HTTP ingenuo nao modelaria."""

    def test_nao_aprova_e_nao_nega(self) -> None:
        resultado = scoring.aplicar_gate_kyc(
            parecer_aprovado(), ResultadoKYC.nao_consultado("timeout apos 2 tentativas")
        )

        assert resultado.decisao is Decisao.ANALISE_MANUAL
        assert resultado.decisao is not Decisao.NEGADO

    def test_o_motivo_chega_na_justificativa(self) -> None:
        """Sem o motivo, quem revisa abre log de tres servicos para descobrir."""
        resultado = scoring.aplicar_gate_kyc(
            parecer_aprovado(), ResultadoKYC.nao_consultado("circuito aberto")
        )

        texto = " ".join(resultado.justificativas)
        assert "circuito aberto" in texto
        assert "indisponibilidade interna" in texto

    def test_o_dominio_distingue_indisponivel_de_reprovado(self) -> None:
        # Se os dois colapsassem, um KYC fora do ar negaria credito — e a
        # diferenca entre "nao verificado" e "reprovado" e a base deste desenho.
        indisponivel = ResultadoKYC.nao_consultado("erro")
        reprovado = ResultadoKYC(decisao=DecisaoKYC.REPROVADO)

        assert indisponivel.indisponivel and not indisponivel.veta
        assert reprovado.veta and not reprovado.indisponivel


class TestDisjuntor:
    def test_comeca_fechado_e_permite(self) -> None:
        d = Disjuntor()

        assert d.estado == EstadoDisjuntor.FECHADO
        assert d.permite()

    def test_abre_depois_do_limite_de_falhas(self) -> None:
        d = Disjuntor(falhas_para_abrir=3)

        for _ in range(3):
            assert d.permite()
            d.registrar_falha()

        assert d.estado == EstadoDisjuntor.ABERTO
        assert not d.permite(), "aberto deve recusar sem tocar na rede"

    def test_sucesso_zera_a_contagem(self) -> None:
        """Falhas **consecutivas**, e nao acumuladas.

        Sem o reset, um servico saudavel com falha ocasional acabaria abrindo o
        circuito depois de horas de operacao normal.
        """
        d = Disjuntor(falhas_para_abrir=3)

        d.registrar_falha()
        d.registrar_falha()
        d.registrar_sucesso()
        d.registrar_falha()
        d.registrar_falha()

        assert d.estado == EstadoDisjuntor.FECHADO

    def test_meio_aberto_libera_uma_sondagem_e_fecha_no_sucesso(self) -> None:
        # Espera zero para nao acoplar o teste ao relogio.
        d = Disjuntor(falhas_para_abrir=1, espera_para_testar=0.0)
        d.registrar_falha()

        assert d.permite(), "passada a janela, uma requisicao deve sondar"
        assert d.estado == EstadoDisjuntor.MEIO_ABERTO

        d.registrar_sucesso()
        assert d.estado == EstadoDisjuntor.FECHADO

    def test_falha_na_sondagem_reabre(self) -> None:
        """Nao reabre a torneira num servico que ainda esta se recuperando."""
        d = Disjuntor(falhas_para_abrir=1, espera_para_testar=0.0)
        d.registrar_falha()
        d.permite()  # entra em meio-aberto
        d.registrar_falha()

        assert d.estado == EstadoDisjuntor.ABERTO


class TestClienteHttp:
    """Testes com transporte simulado — sem rede, sem servico de verdade."""

    def cliente(self, handler: object, **kwargs: object) -> ClienteKYCHttp:
        c = ClienteKYCHttp(url_base="http://kyc-teste", **kwargs)  # type: ignore[arg-type]
        # Injeta o transporte do httpx: exercita o codigo real de requisicao,
        # serializacao e tratamento de status, sem abrir socket.
        c._cliente = httpx.AsyncClient(
            base_url="http://kyc-teste",
            transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        )
        return c

    async def test_traduz_resposta_de_sucesso(self) -> None:
        def responder(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                201,
                json={
                    "id": "8c1f9e6a-0b3d-4a2e-9f11-6c5d4e3b2a10",
                    "decisao": "aprovado_com_diligencia",
                    "nivel_risco": "medio",
                    "justificativas": ["Pessoa Exposta Politicamente"],
                },
            )

        resultado = await self.cliente(responder).triar("Jose da Silva", "52998224725")

        assert resultado.decisao is DecisaoKYC.APROVADO_COM_DILIGENCIA
        assert resultado.triagem_id == "8c1f9e6a-0b3d-4a2e-9f11-6c5d4e3b2a10"
        assert resultado.justificativas == ("Pessoa Exposta Politicamente",)

    async def test_timeout_vira_indisponivel_e_nao_excecao(self) -> None:
        """O contrato central do port.

        Se este metodo levantasse, o caso de uso marcaria a analise como FALHA — e
        a analise nao falhou, ela ficou incompleta.
        """

        def responder(_: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("sem resposta")

        resultado = await self.cliente(responder, tentativas=1).triar("Jose", "52998224725")

        assert resultado.indisponivel
        assert "Timeout" in (resultado.motivo_indisponibilidade or "")

    async def test_retenta_erro_transitorio(self) -> None:
        chamadas: list[int] = []

        def responder(_: httpx.Request) -> httpx.Response:
            chamadas.append(1)
            if len(chamadas) == 1:
                return httpx.Response(503, text="indisponivel")
            return httpx.Response(201, json={"decisao": "aprovado"})

        resultado = await self.cliente(responder, tentativas=2).triar("Jose", "52998224725")

        assert resultado.decisao is DecisaoKYC.APROVADO
        assert len(chamadas) == 2

    async def test_nao_retenta_erro_de_contrato(self) -> None:
        """4xx nao melhora na segunda tentativa — repetir so multiplica carga."""
        chamadas: list[int] = []

        def responder(_: httpx.Request) -> httpx.Response:
            chamadas.append(1)
            return httpx.Response(422, json={"codigo": "payload_invalido"})

        resultado = await self.cliente(responder, tentativas=3).triar("Jo", "52998224725")

        assert resultado.indisponivel
        assert len(chamadas) == 1, "erro permanente nao deve ser retentado"

    async def test_circuito_abre_e_para_de_chamar(self) -> None:
        """A protecao contra falha em cascata, medida em chamadas evitadas."""
        chamadas: list[int] = []

        def responder(_: httpx.Request) -> httpx.Response:
            chamadas.append(1)
            raise httpx.ConnectError("recusada")

        cliente = self.cliente(responder, tentativas=1, disjuntor=Disjuntor(falhas_para_abrir=2))

        for _ in range(5):
            resultado = await cliente.triar("Jose", "52998224725")
            assert resultado.indisponivel

        assert cliente.estado_disjuntor == EstadoDisjuntor.ABERTO
        assert len(chamadas) == 2, (
            "depois de abrir, o cliente nao deve mais tocar na rede — "
            f"foram {len(chamadas)} chamadas"
        )

    async def test_propaga_o_request_id(self) -> None:
        """Fecha a corrente de correlacao entre os dois servicos."""
        import structlog

        recebidos: list[str | None] = []

        def responder(request: httpx.Request) -> httpx.Response:
            recebidos.append(request.headers.get("X-Request-ID"))
            return httpx.Response(201, json={"decisao": "aprovado"})

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id="req-da-analise-123")
        try:
            await self.cliente(responder).triar("Jose", "52998224725")
        finally:
            structlog.contextvars.clear_contextvars()

        assert recebidos == ["req-da-analise-123"]


class TestTraducaoEstrita:
    def test_decisao_desconhecida_e_erro_visivel(self) -> None:
        """Se o outro servico adicionar um estado, isto aparece.

        Mapear silenciosamente para "aprovado" seria a pior forma possivel de
        descobrir uma mudanca de contrato.
        """
        with pytest.raises(_ErroPermanente, match="desconhecida"):
            _traduzir({"decisao": "aprovado_condicionalmente"})

    def test_resposta_sem_decisao_e_erro(self) -> None:
        with pytest.raises(_ErroPermanente, match="sem o campo"):
            _traduzir({"nivel_risco": "baixo"})

    def test_estado_local_vindo_de_fora_e_erro(self) -> None:
        # `INDISPONIVEL` existe apenas nesta esteira. Receber isso do outro servico
        # significa contrato divergente, nao indisponibilidade.
        with pytest.raises(_ErroPermanente, match="so existe localmente"):
            _traduzir({"decisao": "indisponivel"})

    def test_campos_opcionais_ausentes_nao_quebram(self) -> None:
        resultado = _traduzir({"decisao": "aprovado"})

        assert resultado.decisao is DecisaoKYC.APROVADO
        assert resultado.justificativas == ()
        assert resultado.triagem_id is None


class TestFake:
    async def test_registra_as_consultas(self) -> None:
        fake = KYCFake()

        await fake.triar("Jose da Silva", "52998224725")

        assert fake.consultas == [("Jose da Silva", "52998224725")]

    async def test_aprova_por_padrao(self) -> None:
        # Padrao permissivo de proposito: os 360 testes que existiam antes do gate
        # nao devem mudar de resultado por causa dele.
        assert (await KYCFake().triar("Jose", "52998224725")).decisao is DecisaoKYC.APROVADO
