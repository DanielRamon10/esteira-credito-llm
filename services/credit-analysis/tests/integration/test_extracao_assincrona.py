"""O fluxo assincrono de documento, de ponta a ponta.

## O que esta suite protege

Nao e "o 202 responde 202". Sao as tres garantias que fazem uma fila valer a pena, e cada uma
tem um teste que falha se ela cair:

1. **trabalho nao se perde** — falha transitoria devolve a mensagem, e ela e reprocessada;
2. **trabalho nao se duplica** — reentrega depois da conclusao nao tem efeito;
3. **trabalho nao fica preso** — falha permanente termina em estado terminal com motivo, e o
   documento nao volta para a fila para sempre.

A terceira e a mais facil de quebrar sem perceber: um `except Exception` que devolve tudo para a
fila satisfaz (1) e (2) e transforma um PDF corrompido em laco infinito.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from credit_analysis.application.use_cases.extracao_assincrona import ExtrairDocumento
from credit_analysis.application.use_cases.processar_documento import AplicarExtracao
from credit_analysis.application.use_cases.trabalhador import Trabalhador
from credit_analysis.config import Settings
from credit_analysis.domain.armazenamento import EstadoDocumento, Referencia
from credit_analysis.domain.documento import ResultadoOCR
from credit_analysis.domain.enums import TipoDocumento
from credit_analysis.domain.extracao_assincrona import (
    VERSAO_DO_CONTRATO,
    ContratoIncompativel,
    PedidoDeExtracao,
)
from credit_analysis.domain.value_objects import Percentual
from credit_analysis.infrastructure.armazenamento.memoria import (
    ArmazenamentoEmMemoria,
    FilaEmMemoria,
)
from credit_analysis.infrastructure.bureau import BureauSempreLimpo
from credit_analysis.infrastructure.repositories.memoria import RepositorioAnalisesMemoria
from tests.conftest import emitir_token, montar_cliente

pytestmark = pytest.mark.integration

HOLERITE = (
    "EMPRESA EXEMPLO LTDA\n"
    "SALARIO BRUTO 9.000,00\n"
    "TOTAL DESCONTOS 1.800,00\n"
    "SALARIO LIQUIDO 7.200,00\n"
)


class OCRFake:
    """Motor de OCR controlavel. `erro` permite exercitar as duas categorias de falha."""

    def __init__(
        self,
        texto: str = HOLERITE,
        confianca: float = 94,
        erro: Exception | None = None,
    ) -> None:
        self.texto = texto
        self.confianca = confianca
        self.erro = erro
        self.chamadas = 0

    async def extrair(self, imagem: Any) -> ResultadoOCR:
        self.chamadas += 1
        if self.erro is not None:
            raise self.erro
        return ResultadoOCR(
            texto=self.texto,
            confianca=Percentual.de(str(self.confianca)),
            motor="fake",
            palavras_reconhecidas=len(self.texto.split()),
        )

    @property
    def identificacao(self) -> str:
        return "fake"

    @property
    def custo_relativo(self) -> int:
        return 0


def imagem_png() -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(np.full((40, 60), 255, dtype=np.uint8)).save(buffer, "PNG")
    return buffer.getvalue()


@pytest.fixture
def ambiente(settings_teste: Settings, chaves_de_teste: Path) -> dict[str, Any]:
    """Monta API, armazenamento, fila e trabalhador compartilhando o mesmo estado.

    Devolve um dicionario e nao uma tupla de seis itens: `ambiente["fila"]` diz o que e, e
    `ambiente[3]` nao.
    """
    repositorio = RepositorioAnalisesMemoria()
    armazenamento = ArmazenamentoEmMemoria()
    fila = FilaEmMemoria(teto_de_tentativas=3)
    ocr = OCRFake()

    from credit_analysis.api.app import criar_app

    app = criar_app(
        settings=settings_teste,
        repositorio=repositorio,
        bureau=BureauSempreLimpo(),
        motor_ocr=ocr,  # type: ignore[arg-type]
        armazenamento=armazenamento,
        fila=fila,
    )
    trabalhador = Trabalhador(
        fila=fila,
        extrair=ExtrairDocumento(armazenamento=armazenamento, motor_ocr=ocr),  # type: ignore[arg-type]
        aplicar=AplicarExtracao(repositorio=repositorio, bureau=BureauSempreLimpo()),
        repositorio=repositorio,
    )
    token = emitir_token(chaves_de_teste)
    return {
        "app": app,
        "cliente": montar_cliente(app, token),
        "repositorio": repositorio,
        "armazenamento": armazenamento,
        "fila": fila,
        "ocr": ocr,
        "trabalhador": trabalhador,
    }


def criar_analise(cliente: TestClient, payload: dict[str, object]) -> str:
    resposta = cliente.post("/v1/analises", json=payload)
    assert resposta.status_code == 201, resposta.text
    return str(resposta.json()["id"])


def enviar(cliente: TestClient, analise_id: str) -> dict[str, Any]:
    resposta = cliente.post(
        f"/v1/analises/{analise_id}/documentos",
        files={"arquivo": ("holerite.png", imagem_png(), "image/png")},
        data={"tipo": "holerite"},
    )
    assert resposta.status_code == 202, resposta.text
    return dict(resposta.json())


class TestRecepcao:
    def test_devolve_202_com_location(
        self, ambiente: dict[str, Any], payload_analise: dict[str, object]
    ) -> None:
        """RFC 7231 secao 6.3.2. Sem `Location`, um 202 nao diz como acompanhar."""
        with ambiente["cliente"] as cliente:
            analise_id = criar_analise(cliente, payload_analise)
            resposta = cliente.post(
                f"/v1/analises/{analise_id}/documentos",
                files={"arquivo": ("holerite.png", imagem_png(), "image/png")},
                data={"tipo": "holerite"},
            )

        assert resposta.status_code == 202
        assert resposta.headers["Location"] == resposta.json()["consultar_em"]

    def test_o_ocr_nao_roda_na_requisicao(
        self, ambiente: dict[str, Any], payload_analise: dict[str, object]
    ) -> None:
        """A razao de existir da camada inteira.

        Se o OCR rodasse na requisicao, o cliente esperaria os segundos da extracao e um gateway
        decidiria o timeout. Este teste falha se alguem "otimizar" chamando o motor na borda.
        """
        with ambiente["cliente"] as cliente:
            analise_id = criar_analise(cliente, payload_analise)
            enviar(cliente, analise_id)

        assert ambiente["ocr"].chamadas == 0

    def test_o_documento_e_guardado_e_enfileirado(
        self, ambiente: dict[str, Any], payload_analise: dict[str, object]
    ) -> None:
        with ambiente["cliente"] as cliente:
            analise_id = criar_analise(cliente, payload_analise)
            enviar(cliente, analise_id)

        assert ambiente["armazenamento"].total == 1
        assert ambiente["fila"].pendentes == 1

    def test_estado_inicial_e_recebido_e_nao_terminal(
        self, ambiente: dict[str, Any], payload_analise: dict[str, object]
    ) -> None:
        """`terminal: False` diz ao cliente que vale voltar a perguntar."""
        with ambiente["cliente"] as cliente:
            analise_id = criar_analise(cliente, payload_analise)
            corpo = enviar(cliente, analise_id)
            estado = cliente.get(corpo["consultar_em"]).json()

        assert estado["estado"] == EstadoDocumento.RECEBIDO
        assert estado["terminal"] is False

    def test_arquivo_vazio_e_recusado_antes_de_guardar(
        self, ambiente: dict[str, Any], payload_analise: dict[str, object]
    ) -> None:
        """Gravar zero byte e enfileirar produziria falha de extracao para algo que a borda
        podia recusar de imediato."""
        with ambiente["cliente"] as cliente:
            analise_id = criar_analise(cliente, payload_analise)
            resposta = cliente.post(
                f"/v1/analises/{analise_id}/documentos",
                files={"arquivo": ("vazio.png", b"", "image/png")},
                data={"tipo": "holerite"},
            )

        assert resposta.status_code == 422
        assert ambiente["armazenamento"].total == 0
        assert ambiente["fila"].pendentes == 0


class TestCicloCompleto:
    async def test_extracao_muda_o_score(
        self, ambiente: dict[str, Any], payload_analise: dict[str, object]
    ) -> None:
        """O ponto do documento existir.

        Sem esta assercao, a extracao poderia virar metadado decorativo: o estado avancaria para
        `extraido` e o score continuaria baseado no valor **declarado** pelo proprio solicitante.
        """
        with ambiente["cliente"] as cliente:
            analise_id = criar_analise(cliente, payload_analise)
            score_antes = cliente.get(f"/v1/analises/{analise_id}").json()["parecer"]["score"]
            corpo = enviar(cliente, analise_id)

            await ambiente["trabalhador"].drenar()

            estado = cliente.get(corpo["consultar_em"]).json()
            score_depois = cliente.get(f"/v1/analises/{analise_id}").json()["parecer"]["score"]

        assert estado["estado"] == EstadoDocumento.EXTRAIDO
        assert estado["terminal"] is True
        assert score_depois != score_antes

    async def test_a_fila_esvazia(
        self, ambiente: dict[str, Any], payload_analise: dict[str, object]
    ) -> None:
        with ambiente["cliente"] as cliente:
            analise_id = criar_analise(cliente, payload_analise)
            enviar(cliente, analise_id)
            await ambiente["trabalhador"].drenar()

        assert ambiente["fila"].pendentes == 0
        assert ambiente["fila"].em_voo == 0

    async def test_qualidade_baixa_vira_estado_rejeitado_e_nao_erro_http(
        self, ambiente: dict[str, Any], payload_analise: dict[str, object]
    ) -> None:
        """A troca que a assincronia impos, e o limite dela.

        Antes, confianca abaixo do piso da POL-002 devolvia 422 com a instrucao de reenviar.
        Agora o 202 ja foi dado — e a instrucao **precisa** chegar por outro caminho. Este teste
        garante que ela chega integralmente, com o DPI minimo e a referencia a politica.

        Se o motivo virasse "erro de extracao", o cliente nao saberia que a acao dele e reenviar
        com mais resolucao, e a rejeicao seria indistinguivel de falha do servico.
        """
        ambiente["ocr"].confianca = 41

        with ambiente["cliente"] as cliente:
            analise_id = criar_analise(cliente, payload_analise)
            corpo = enviar(cliente, analise_id)
            await ambiente["trabalhador"].drenar()
            estado = cliente.get(corpo["consultar_em"]).json()

        assert estado["estado"] == EstadoDocumento.REJEITADO
        assert estado["terminal"] is True
        assert "200 DPI" in estado["erro"]
        assert "POL-002" in estado["erro"]


class TestTrabalhoNaoSePerde:
    async def test_falha_transitoria_devolve_e_reprocessa(
        self, ambiente: dict[str, Any], payload_analise: dict[str, object]
    ) -> None:
        """A garantia pela qual se escolhe uma fila.

        O OCR falha na primeira passada e funciona na segunda. Sem devolucao, o documento ficaria
        `extraindo` para sempre e o trabalho estaria perdido — com o cliente vendo "processando".
        """
        with ambiente["cliente"] as cliente:
            analise_id = criar_analise(cliente, payload_analise)
            corpo = enviar(cliente, analise_id)

            ambiente["ocr"].erro = TimeoutError("motor de OCR indisponivel")
            await ambiente["trabalhador"].processar_lote()

            assert cliente.get(corpo["consultar_em"]).json()["estado"] != EstadoDocumento.EXTRAIDO
            assert ambiente["fila"].pendentes == 1, "a mensagem nao voltou para a fila"

            ambiente["ocr"].erro = None
            await ambiente["trabalhador"].drenar()

            assert cliente.get(corpo["consultar_em"]).json()["estado"] == EstadoDocumento.EXTRAIDO

    async def test_teto_de_tentativas_manda_para_descarte(
        self, ambiente: dict[str, Any], payload_analise: dict[str, object]
    ) -> None:
        """Devolucao sem teto ocuparia o trabalhador para sempre.

        Com falha transitoria permanente (um OCR que nunca volta), o teto e o que impede o laco
        infinito. O `drenar` tem a propria guarda de passadas, e este teste confirma que ela nao
        e a que segura: a fila descarta antes.
        """
        with ambiente["cliente"] as cliente:
            analise_id = criar_analise(cliente, payload_analise)
            enviar(cliente, analise_id)
            ambiente["ocr"].erro = TimeoutError("nunca volta")

            await ambiente["trabalhador"].drenar()

        assert ambiente["fila"].pendentes == 0
        descartadas = ambiente["fila"].descartadas
        assert len(descartadas) == 1
        assert "TimeoutError" in descartadas[0][1]


class TestTrabalhoNaoSeDuplica:
    async def test_reentrega_depois_da_conclusao_nao_tem_efeito(
        self, ambiente: dict[str, Any], payload_analise: dict[str, object]
    ) -> None:
        """Idempotencia, e o que ela protege nao e o score.

        O score sairia igual de qualquer forma (mesma renda, mesmo calculo). O que a reaplicacao
        estragaria e a **trilha**: `reabrir_para_reavaliacao` incrementaria o contador por um
        evento que nao aconteceu, e a auditoria mostraria uma reabertura que ninguem pediu.
        """
        with ambiente["cliente"] as cliente:
            analise_id = criar_analise(cliente, payload_analise)
            corpo = enviar(cliente, analise_id)
            await ambiente["trabalhador"].drenar()

            # Le do repositorio e nao da API: `reavaliacoes` e um contador de **auditoria** e nao
            # esta no schema de resposta de proposito — ele existe para a trilha, nao para o
            # consumidor. Ler do dominio e o que a assercao de fato quer.
            analise = await ambiente["repositorio"].buscar_por_id(UUID(analise_id))
            assert analise is not None
            reavaliacoes = analise.reavaliacoes
            assert analise.parecer is not None
            score = analise.parecer.score

            # Reentrega o mesmo pedido, como o SQS faria.
            documento = analise.documentos[0]
            assert documento.referencia is not None
            await ambiente["fila"].publicar(
                PedidoDeExtracao(
                    analise_id=UUID(analise_id),
                    documento_id=UUID(corpo["documento_id"]),
                    referencia=documento.referencia,
                    tipo=TipoDocumento.HOLERITE,
                    nome_arquivo="holerite.png",
                )
            )
            await ambiente["trabalhador"].drenar()

            final = await ambiente["repositorio"].buscar_por_id(UUID(analise_id))

        assert final is not None
        assert final.reavaliacoes == reavaliacoes, "a reaplicacao reabriu a analise"
        assert final.parecer is not None
        assert final.parecer.score == score

    async def test_a_reaplicacao_nao_roda_ocr_de_novo(
        self, ambiente: dict[str, Any], payload_analise: dict[str, object]
    ) -> None:
        """A guarda de estado terminal esta em `AplicarExtracao`, **depois** do OCR.

        Ou seja: a reaplicacao ainda paga o custo da extracao. Isso e uma limitacao real e
        conhecida — evita-la exigiria consultar o estado antes de extrair, o que poria o
        repositorio de volta como dependencia da metade que roda como Lambda.

        O teste documenta o comportamento que existe, e nao o desejado. Se um dia a extracao
        virar caro o suficiente para justificar, a saida e o trabalhador consultar o estado antes
        de chamar `ExtrairDocumento` — no trabalhador, que ja tem repositorio.
        """
        with ambiente["cliente"] as cliente:
            analise_id = criar_analise(cliente, payload_analise)
            corpo = enviar(cliente, analise_id)
            await ambiente["trabalhador"].drenar()
            chamadas_apos_primeira = ambiente["ocr"].chamadas

            analise = await ambiente["repositorio"].buscar_por_id(UUID(analise_id))
            assert analise is not None
            referencia = analise.documentos[0].referencia
            assert referencia is not None
            await ambiente["fila"].publicar(
                PedidoDeExtracao(
                    analise_id=UUID(analise_id),
                    documento_id=UUID(corpo["documento_id"]),
                    referencia=referencia,
                    tipo=TipoDocumento.HOLERITE,
                    nome_arquivo="holerite.png",
                )
            )
            await ambiente["trabalhador"].drenar()

        assert ambiente["ocr"].chamadas == chamadas_apos_primeira + 1


class TestTrabalhoNaoFicaPreso:
    async def test_arquivo_corrompido_termina_em_falhou(
        self, ambiente: dict[str, Any], payload_analise: dict[str, object]
    ) -> None:
        """Falha permanente, e o teste que um `except Exception` generico quebraria.

        Um PDF corrompido falha igual nas cinquenta tentativas. Devolvendo para a fila, ele
        ocuparia o trabalhador para sempre — e num sistema com capacidade finita, tomaria a vaga
        dos documentos que dariam certo.
        """
        with ambiente["cliente"] as cliente:
            analise_id = criar_analise(cliente, payload_analise)
            resposta = cliente.post(
                f"/v1/analises/{analise_id}/documentos",
                files={"arquivo": ("quebrado.png", b"isto-nao-e-uma-imagem", "image/png")},
                data={"tipo": "holerite"},
            )
            assert resposta.status_code == 202, "a borda nao decodifica: quem falha e a extracao"
            corpo = resposta.json()

            await ambiente["trabalhador"].drenar()
            estado = cliente.get(corpo["consultar_em"]).json()

        assert estado["estado"] == EstadoDocumento.FALHOU
        assert estado["terminal"] is True
        assert "ErroLeituraDocumento" in estado["erro"]
        assert ambiente["fila"].pendentes == 0, "falha permanente nao pode voltar para a fila"

    async def test_referencia_inexistente_e_transitoria_e_nao_permanente(
        self, ambiente: dict[str, Any]
    ) -> None:
        """Objeto ausente pode ser consistencia eventual do armazenamento.

        Classificar como permanente perderia o trabalho de um documento que apareceria um segundo
        depois. O custo de errar para este lado e uma retentativa; o de errar para o outro e um
        documento em revisao humana sem motivo.
        """
        await ambiente["fila"].publicar(
            PedidoDeExtracao(
                analise_id=uuid4(),
                documento_id=uuid4(),
                referencia=Referencia(chave="nao/existe", versao="x"),
                tipo=TipoDocumento.HOLERITE,
                nome_arquivo="fantasma.png",
            )
        )

        await ambiente["trabalhador"].processar_lote()

        assert ambiente["fila"].pendentes == 1


class TestContratoDaMensagem:
    def test_ida_e_volta(self) -> None:
        pedido = PedidoDeExtracao(
            analise_id=uuid4(),
            documento_id=uuid4(),
            referencia=Referencia(chave="documentos/a/b/x.png", versao="v1"),
            tipo=TipoDocumento.EXTRATO_BANCARIO,
            nome_arquivo="x.png",
            request_id="abc-123",
        )

        assert PedidoDeExtracao.de_json(pedido.para_json()) == pedido

    def test_versao_desconhecida_e_recusada(self) -> None:
        """Fila tem estado: durante um deploy, o consumidor novo le mensagem antiga.

        Sem a checagem, um campo renomeado levantaria `KeyError` e o erro apontaria para o
        consumidor em vez da incompatibilidade — e a mensagem entraria em laco de tentativa.
        """
        bruto = '{"versao": 99, "analise_id": "x"}'

        with pytest.raises(ContratoIncompativel, match="99"):
            PedidoDeExtracao.de_json(bruto)

    def test_a_versao_atual_esta_no_json(self) -> None:
        pedido = PedidoDeExtracao(
            analise_id=uuid4(),
            documento_id=uuid4(),
            referencia=Referencia(chave="k", versao="v"),
            tipo=TipoDocumento.HOLERITE,
            nome_arquivo="x.png",
        )

        assert f'"versao": {VERSAO_DO_CONTRATO}' in pedido.para_json()

    def test_o_pedido_nao_carrega_o_conteudo(self) -> None:
        """Mensagem seria copia: uma nova tentativa reprocessaria os bytes da mensagem antiga
        enquanto a auditoria leria os do armazenamento."""
        campos = set(PedidoDeExtracao.__dataclass_fields__)

        assert "conteudo" not in campos
        assert "referencia" in campos
