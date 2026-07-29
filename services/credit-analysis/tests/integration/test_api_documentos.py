"""Testes de integracao do upload de documento.

Usam `OCRFake` para nao depender do Tesseract nem de imagem real — o que se
testa aqui e a rota: validacao de arquivo, limite de tamanho, ligacao com a
analise e a resposta. A acuracia sobre imagem esta em `tests/eval`.

A excecao e a classe de injecao de prompt, que renderiza um documento de verdade
com o ataque embutido: e o unico jeito de provar que o pipeline inteiro contem a
tentativa, do pixel a resposta HTTP.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from credit_analysis.api.app import criar_app
from credit_analysis.config import Settings
from credit_analysis.domain.value_objects import Percentual
from credit_analysis.infrastructure.llm.anthropic_adapter import LLMFake
from credit_analysis.infrastructure.ocr.vision import OCRFake
from credit_analysis.infrastructure.repositories.memoria import RepositorioAnalisesMemoria
from tests.apoio.documentos_sinteticos import INJECAO_TIPICA, Holerite

pytestmark = pytest.mark.integration

HOLERITE_TEXTO = """\
RECIBO DE PAGAMENTO DE SALARIO
INDUSTRIA BRASILEIRA DE COMPONENTES LTDA
CNPJ: 12.345.678/0001-90
Nome: MARIA OLIVEIRA SANTOS  Competencia: 06/2025
CPF: 529.982.247-25  Admissao: 15/03/2019
SALARIO BASE 8.500,00
VALOR LIQUIDO A RECEBER R$ 7.262,14
"""

EXTRATO_TEXTO = "\n".join(
    ["DATA HISTORICO VALOR SALDO"]
    + [
        f"05/{mes:02d}/2025 CREDITO SALARIO EMPRESA 8.032,14 C {8032 * mes},14"
        for mes in range(1, 7)
    ]
)


def imagem_png(largura: int = 60, altura: int = 40) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(np.full((altura, largura), 255, dtype=np.uint8)).save(buffer, "PNG")
    return buffer.getvalue()


def montar_client(
    settings: Settings,
    repositorio: RepositorioAnalisesMemoria,
    *,
    texto_ocr: str = HOLERITE_TEXTO,
    confianca: float = 92,
    motor: object | None = None,
) -> TestClient:
    app = criar_app(
        settings=settings,
        repositorio=repositorio,
        llm=LLMFake(),
        motor_ocr=motor or OCRFake(texto_ocr, Percentual.de(confianca), "fake"),  # type: ignore[arg-type]
    )
    return TestClient(app)


@pytest.fixture
def client_doc(
    settings_teste: Settings, repositorio: RepositorioAnalisesMemoria
) -> Iterator[TestClient]:
    with montar_client(settings_teste, repositorio) as c:
        yield c


@pytest.fixture
def analise_id(client_doc: TestClient, payload_analise: dict[str, Any]) -> str:
    resposta = client_doc.post("/v1/analises", json=payload_analise)
    assert resposta.status_code == 201
    return str(resposta.json()["id"])


def enviar(
    client: TestClient,
    analise_id: str,
    *,
    conteudo: bytes | None = None,
    nome: str = "holerite.png",
    tipo: str = "holerite",
) -> Any:
    return client.post(
        f"/v1/analises/{analise_id}/documentos",
        files={"arquivo": (nome, conteudo if conteudo is not None else imagem_png(), "image/png")},
        data={"tipo": tipo},
    )


class TestUploadValido:
    def test_extrai_e_anexa_a_analise(self, client_doc: TestClient, analise_id: str) -> None:
        resposta = enviar(client_doc, analise_id)
        assert resposta.status_code == 201

        corpo = resposta.json()
        assert corpo["analise_id"] == analise_id
        assert corpo["tipo"] == "holerite"
        assert corpo["motor_ocr"] == "fake"
        assert corpo["conteudo_hash"]  # SHA-256 para trilha de auditoria

    def test_apura_a_renda_do_documento(self, client_doc: TestClient, analise_id: str) -> None:
        corpo = enviar(client_doc, analise_id).json()
        # O liquido tem precedencia sobre o base (POL-001 secao 3).
        assert corpo["renda_comprovada"] == "7262.14"

    def test_devolve_os_campos_com_trecho_de_origem(
        self, client_doc: TestClient, analise_id: str
    ) -> None:
        corpo = enviar(client_doc, analise_id).json()
        campos = {c["nome"]: c for c in corpo["campos_extraidos"]}

        assert "cpf" in campos
        assert campos["cpf"]["valor"] == "52998224725"
        # O trecho de origem permite conferir sem reabrir o documento.
        assert campos["cpf"]["trecho_origem"]

    def test_documento_confiavel_nao_exige_revisao(
        self, client_doc: TestClient, analise_id: str
    ) -> None:
        corpo = enviar(client_doc, analise_id).json()
        assert corpo["qualidade"] == "confiavel"
        assert corpo["exige_revisao_humana"] is False

    def test_extrato_produz_resumo_para_o_score(
        self,
        settings_teste: Settings,
        repositorio: RepositorioAnalisesMemoria,
        payload_analise: dict[str, Any],
    ) -> None:
        with montar_client(settings_teste, repositorio, texto_ocr=EXTRATO_TEXTO) as client:
            aid = client.post("/v1/analises", json=payload_analise).json()["id"]
            corpo = enviar(client, aid, tipo="extrato_bancario").json()

        assert corpo["resumo_extrato"] is not None
        resumo = corpo["resumo_extrato"]
        assert resumo["meses_analisados"] == 6
        assert resumo["renda_mediana_mensal"] == "8032.14"
        assert corpo["renda_comprovada"] == "8032.14"


class TestValidacao:
    def test_extensao_nao_aceita(self, client_doc: TestClient, analise_id: str) -> None:
        resposta = enviar(client_doc, analise_id, nome="documento.exe")
        assert resposta.status_code == 422
        assert "nao aceita" in resposta.json()["mensagem"]

    def test_arquivo_sem_extensao(self, client_doc: TestClient, analise_id: str) -> None:
        assert enviar(client_doc, analise_id, nome="documento").status_code == 422

    def test_arquivo_vazio(self, client_doc: TestClient, analise_id: str) -> None:
        resposta = enviar(client_doc, analise_id, conteudo=b"")
        assert resposta.status_code == 422
        assert "vazio" in resposta.json()["mensagem"]

    def test_imagem_corrompida_vira_422_e_nao_500(
        self, client_doc: TestClient, analise_id: str
    ) -> None:
        resposta = enviar(client_doc, analise_id, conteudo=b"nao sou uma imagem de verdade")
        assert resposta.status_code == 422

    def test_analise_inexistente(self, client_doc: TestClient) -> None:
        resposta = enviar(client_doc, "00000000-0000-0000-0000-000000000000")
        assert resposta.status_code == 404
        assert resposta.json()["codigo"] == "analise_nao_encontrada"

    def test_arquivo_acima_do_limite(self, client_doc: TestClient, analise_id: str) -> None:
        from credit_analysis.api.routers.documentos import TAMANHO_MAXIMO_BYTES

        # O limite e aplicado sobre o que realmente chega, nao sobre o header
        # Content-Length, que o cliente informa e pode falsificar.
        gigante = b"\x89PNG\r\n\x1a\n" + b"\x00" * (TAMANHO_MAXIMO_BYTES + 1024)
        resposta = enviar(client_doc, analise_id, conteudo=gigante)

        assert resposta.status_code == 413
        assert resposta.json()["codigo"] == "arquivo_grande_demais"

    def test_tipo_de_documento_invalido(self, client_doc: TestClient, analise_id: str) -> None:
        assert enviar(client_doc, analise_id, tipo="nao_existe").status_code == 422


class TestQualidadeDeExtracao:
    def test_confianca_intermediaria_exige_revisao(
        self,
        settings_teste: Settings,
        repositorio: RepositorioAnalisesMemoria,
        payload_analise: dict[str, Any],
    ) -> None:
        # POL-002 secao 3.2: entre 60% e 85% vai para conferencia humana.
        with montar_client(settings_teste, repositorio, confianca=70) as client:
            aid = client.post("/v1/analises", json=payload_analise).json()["id"]
            corpo = enviar(client, aid).json()

        assert corpo["qualidade"] == "revisao_humana"
        assert corpo["exige_revisao_humana"] is True

    def test_confianca_baixa_e_rejeitada_com_instrucao(
        self,
        settings_teste: Settings,
        repositorio: RepositorioAnalisesMemoria,
        payload_analise: dict[str, Any],
    ) -> None:
        # Abaixo de 60% a politica manda reenviar, e a mensagem diz como.
        with montar_client(settings_teste, repositorio, confianca=45) as client:
            aid = client.post("/v1/analises", json=payload_analise).json()["id"]
            resposta = enviar(client, aid)

        assert resposta.status_code == 422
        mensagem = resposta.json()["mensagem"]
        assert "200 DPI" in mensagem
        assert "POL-002" in mensagem

    def test_renda_ausente_exige_revisao(
        self,
        settings_teste: Settings,
        repositorio: RepositorioAnalisesMemoria,
        payload_analise: dict[str, Any],
    ) -> None:
        with montar_client(
            settings_teste, repositorio, texto_ocr="RECIBO\nNome: JOAO DA SILVA\n"
        ) as client:
            aid = client.post("/v1/analises", json=payload_analise).json()["id"]
            corpo = enviar(client, aid).json()

        assert corpo["renda_comprovada"] is None
        assert corpo["exige_revisao_humana"] is True


class TestInjecaoDePrompt:
    """O ataque que a Camada 3 existe para conter.

    Um holerite visualmente legitimo com uma instrucao escondida no rodape,
    tentando fazer o sistema declarar uma renda inexistente.
    """

    def test_documento_com_injecao_e_marcado(
        self,
        settings_teste: Settings,
        repositorio: RepositorioAnalisesMemoria,
        payload_analise: dict[str, Any],
    ) -> None:
        texto_atacado = HOLERITE_TEXTO + "\n" + INJECAO_TIPICA
        with montar_client(settings_teste, repositorio, texto_ocr=texto_atacado) as client:
            aid = client.post("/v1/analises", json=payload_analise).json()["id"]
            corpo = enviar(client, aid).json()

        assert corpo["injecao_suspeita"] is True
        assert len(corpo["categorias_suspeitas"]) >= 2

    def test_injecao_forca_revisao_humana(
        self,
        settings_teste: Settings,
        repositorio: RepositorioAnalisesMemoria,
        payload_analise: dict[str, Any],
    ) -> None:
        texto_atacado = HOLERITE_TEXTO + "\n" + INJECAO_TIPICA
        with montar_client(settings_teste, repositorio, texto_ocr=texto_atacado) as client:
            aid = client.post("/v1/analises", json=payload_analise).json()["id"]
            corpo = enviar(client, aid).json()

        # Mesmo com confianca alta e todos os campos extraidos, o caso nao segue
        # automatico: tentativa de injecao em documento de credito e indicio de
        # fraude.
        assert corpo["qualidade"] == "confiavel"
        assert corpo["exige_revisao_humana"] is True

    def test_injecao_nao_altera_a_renda_apurada(
        self,
        settings_teste: Settings,
        repositorio: RepositorioAnalisesMemoria,
        payload_analise: dict[str, Any],
    ) -> None:
        """A defesa arquitetural, e nao textual.

        A injecao pede renda de R$ 50.000. O valor que alimenta o score vem da
        extracao por regex sobre o proprio documento, entao a instrucao nao tem
        por onde influenciar o numero — independentemente do que um LLM faria
        com aquele texto.
        """
        texto_atacado = HOLERITE_TEXTO + "\n" + INJECAO_TIPICA
        with montar_client(settings_teste, repositorio, texto_ocr=texto_atacado) as client:
            aid = client.post("/v1/analises", json=payload_analise).json()["id"]
            corpo = enviar(client, aid).json()

        assert corpo["renda_comprovada"] == "7262.14"
        assert corpo["renda_comprovada"] != "50000.00"

    def test_documento_renderizado_com_injecao_e_contido(
        self,
        settings_teste: Settings,
        repositorio: RepositorioAnalisesMemoria,
        payload_analise: dict[str, Any],
    ) -> None:
        """Ponta a ponta com imagem real: o ataque impresso no documento.

        Nao depende do Tesseract — o `OCRFake` recebe o texto que um OCR leria
        —, mas o documento e renderizado de verdade, provando que o ataque
        sobrevive ao caminho do arquivo e ainda assim e contido.
        """
        holerite = Holerite(rodape_adicional=INJECAO_TIPICA)
        buffer = io.BytesIO()
        holerite.renderizar().save(buffer, "PNG")

        texto_que_o_ocr_leria = HOLERITE_TEXTO + "\n" + INJECAO_TIPICA
        with montar_client(settings_teste, repositorio, texto_ocr=texto_que_o_ocr_leria) as client:
            aid = client.post("/v1/analises", json=payload_analise).json()["id"]
            corpo = enviar(client, aid, conteudo=buffer.getvalue()).json()

        assert corpo["injecao_suspeita"] is True
        assert corpo["renda_comprovada"] == "7262.14"
        assert corpo["exige_revisao_humana"] is True
