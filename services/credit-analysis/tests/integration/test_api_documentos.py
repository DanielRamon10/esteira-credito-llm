"""Testes de integracao do upload de documento.

## O que mudou com a Camada 8

O endpoint devolve **202** e a extracao roda num trabalhador. Cada teste continua afirmando o
mesmo fato de negocio — renda apurada, campos extraidos, injecao detectada — mas agora sobre o
estado **final**, depois de drenar a fila, em vez da resposta imediata.

Ha dois helpers de proposito. `enviar` faz o POST cru e serve aos testes de validacao, que
esperam 422 ou 413 e nunca chegam a extrair. `enviar_e_processar` faz POST, drena e devolve o
GET — usa-lo nos testes de validacao seria pior: eles falhariam no `assert 202` com uma mensagem
sobre o passo errado.

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
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from credit_analysis.api.app import criar_app
from credit_analysis.application.use_cases.extracao_assincrona import ExtrairDocumento
from credit_analysis.application.use_cases.processar_documento import AplicarExtracao
from credit_analysis.application.use_cases.trabalhador import Trabalhador
from credit_analysis.config import Settings
from credit_analysis.domain.value_objects import Percentual
from credit_analysis.infrastructure.armazenamento.memoria import (
    ArmazenamentoEmMemoria,
    FilaEmMemoria,
)
from credit_analysis.infrastructure.bureau import BureauSempreLimpo
from credit_analysis.infrastructure.llm.anthropic_adapter import LLMFake
from credit_analysis.infrastructure.ocr.vision import OCRFake
from credit_analysis.infrastructure.repositories.memoria import RepositorioAnalisesMemoria
from tests.apoio.documentos_sinteticos import INJECAO_TIPICA, Holerite
from tests.conftest import emitir_token, montar_cliente

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
    chaves: Path,
    *,
    texto_ocr: str = HOLERITE_TEXTO,
    confianca: float = 92,
    motor: object | None = None,
) -> TestClient:
    ocr = motor or OCRFake(texto_ocr, Percentual.de(confianca), "fake")
    armazenamento = ArmazenamentoEmMemoria()
    fila = FilaEmMemoria()

    app = criar_app(
        settings=settings,
        repositorio=repositorio,
        llm=LLMFake(),
        motor_ocr=ocr,  # type: ignore[arg-type]
        armazenamento=armazenamento,
        fila=fila,
    )
    # O trabalhador vai pendurado no cliente, e nao devolvido a parte: os testes o usam sempre
    # junto, e uma tupla faria cada `with` virar `with ...[0] as c`, que esconde qual e qual.
    cliente = montar_cliente(app, emitir_token(chaves))
    cliente.trabalhador = Trabalhador(  # type: ignore[attr-defined]
        fila=fila,
        extrair=ExtrairDocumento(armazenamento=armazenamento, motor_ocr=ocr),  # type: ignore[arg-type]
        aplicar=AplicarExtracao(repositorio=repositorio, bureau=BureauSempreLimpo()),
        repositorio=repositorio,
    )
    return cliente


@pytest.fixture
def client_doc(
    settings_teste: Settings, repositorio: RepositorioAnalisesMemoria, chaves_de_teste: Path
) -> Iterator[TestClient]:
    with montar_client(settings_teste, repositorio, chaves_de_teste) as c:
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


async def enviar_e_processar(
    client: TestClient,
    analise_id: str,
    *,
    conteudo: bytes | None = None,
    nome: str = "holerite.png",
    tipo: str = "holerite",
) -> dict[str, Any]:
    """POST, drena a fila e devolve o corpo do GET.

    Devolve o payload da **consulta** e nao o do 202: o 202 carrega apenas estado e o caminho de
    acompanhamento, e o que os testes verificam e o resultado da extracao.
    """
    resposta = enviar(client, analise_id, conteudo=conteudo, nome=nome, tipo=tipo)
    assert resposta.status_code == 202, resposta.text

    await client.trabalhador.drenar()  # type: ignore[attr-defined]

    consulta = client.get(resposta.json()["consultar_em"])
    assert consulta.status_code == 200, consulta.text
    return dict(consulta.json())


class TestUploadValido:
    async def test_extrai_e_anexa_a_analise(self, client_doc: TestClient, analise_id: str) -> None:
        corpo = await enviar_e_processar(client_doc, analise_id)

        assert corpo["analise_id"] == analise_id
        assert corpo["tipo"] == "holerite"
        assert corpo["estado"] == "extraido"
        # `motor_ocr` e **persistido** desde a Camada 8. Antes ele so existia na resposta
        # sincrona; agora responde "qual motor leu este documento?" depois do restart.
        assert corpo["motor_ocr"] == "fake"

    async def test_o_hash_do_conteudo_fica_na_trilha(
        self, client_doc: TestClient, analise_id: str, repositorio: RepositorioAnalisesMemoria
    ) -> None:
        """O SHA-256 nao esta na resposta do GET, e isso e uma escolha.

        Ele serve a auditoria e nao a quem faz polling: o cliente que enviou o arquivo ja pode
        calcula-lo. Expo-lo daria a um chamador com `analises:ler` a capacidade de confirmar se
        um conteudo especifico foi submetido — um oraculo pequeno, mas sem contrapartida.

        A assercao passou a ser sobre o dominio, que e onde o dado vive.
        """
        await enviar_e_processar(client_doc, analise_id)

        analise = await repositorio.buscar_por_id(UUID(analise_id))
        assert analise is not None
        assert analise.documentos[0].conteudo_hash

    async def test_apura_a_renda_do_documento(
        self, client_doc: TestClient, analise_id: str
    ) -> None:
        corpo = await enviar_e_processar(client_doc, analise_id)
        # O liquido tem precedencia sobre o base (POL-001 secao 3).
        assert corpo["renda_comprovada"] == "7262.14"

    async def test_devolve_os_dados_com_procedencia(
        self, client_doc: TestClient, analise_id: str
    ) -> None:
        """Os dados extraidos, com origem e confianca.

        ## O que este teste perdeu, e por que

        A versao sincrona afirmava `campos["cpf"]["trecho_origem"]` — o pedaco do texto de onde o
        campo saiu, para conferir sem reabrir o documento. Essa informacao **nunca foi
        persistida**: ela vivia em `CampoExtraido`, montado a cada requisicao, enquanto o que vai
        para o banco e `DadoExtraido`, que nao tem o campo.

        Ou seja, o trecho de origem ja se perdia no primeiro restart, antes da Camada 8 — o fluxo
        assincrono apenas tornou a lacuna visivel, porque agora a unica leitura possivel e a
        persistida. Adicionar `trecho_origem` a `DadoExtraido` e uma mudanca de dominio que vale
        discutir por si, e nao empurrar junto de outra camada.

        O que sobra e o que sustenta o parecer: valor, origem e confianca.
        """
        corpo = await enviar_e_processar(client_doc, analise_id)
        dados = {d["campo"]: d for d in corpo["dados_extraidos"]}

        assert "cpf" in dados
        assert dados["cpf"]["valor"] == "52998224725"
        assert dados["cpf"]["origem"] == "ocr"
        assert Decimal(dados["cpf"]["confianca_pct"]) > 0

    async def test_documento_confiavel_nao_exige_revisao(
        self, client_doc: TestClient, analise_id: str
    ) -> None:
        """`exige_revisao_humana` e **persistido**, e nao derivado no cliente.

        E decisao de politica (POL-002 secao 3.2) e combina tres gatilhos: faixa de qualidade,
        injecao detectada e ausencia de renda apurada. Deixar o cliente derivar da confianca
        reproduziria a regra fora do dominio, e ela mudaria em dois lugares.

        `qualidade` saiu da resposta: ela e funcao da confianca, e devolver os dois convidaria um
        cliente a comparar e discordar. A confianca fica; o veredito de politica tambem.
        """
        corpo = await enviar_e_processar(client_doc, analise_id)

        assert corpo["estado"] == "extraido"
        assert corpo["exige_revisao_humana"] is False
        assert Decimal(corpo["confianca_ocr_pct"]) >= 80

    async def test_extrato_produz_resumo_para_o_score(
        self,
        settings_teste: Settings,
        repositorio: RepositorioAnalisesMemoria,
        payload_analise: dict[str, Any],
        chaves_de_teste: Path,
    ) -> None:
        """O extrato apura renda pela mediana, e os meses entram no score.

        ## O que saiu da resposta

        O objeto `resumo_extrato` inteiro — saldo medio, recorrentes, dispersao. Ele era
        **derivado e nao persistido**: existia so na resposta sincrona. O que vai para o banco sao
        os `DadoExtraido` que alimentaram o calculo, e e sobre eles que este teste passou a
        afirmar.

        A diferenca pratica e pequena porque o que importava ja esta aqui: a mediana (POL-005
        secao 3, nao a media) e o numero de meses. O resto era detalhe de apoio.
        """
        with montar_client(
            settings_teste, repositorio, chaves_de_teste, texto_ocr=EXTRATO_TEXTO
        ) as client:
            aid = client.post("/v1/analises", json=payload_analise).json()["id"]
            corpo = await enviar_e_processar(client, aid, tipo="extrato_bancario")

        dados = {d["campo"]: d["valor"] for d in corpo["dados_extraidos"]}

        assert dados["meses_historico_bancario"] == "6"
        assert dados["renda_mediana_extrato"] == "8032.14"
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

    async def test_imagem_corrompida_termina_em_falhou(
        self, client_doc: TestClient, analise_id: str
    ) -> None:
        """A validacao de conteudo mudou de lugar, e o codigo mudou com ela.

        Antes: a borda decodificava a imagem na requisicao, e um arquivo corrompido virava **422**
        na hora. Agora a borda so confere a extensao — decodificar exigiria ler o arquivo inteiro
        na requisicao, que e exatamente o custo que o 202 existe para evitar.

        O resultado e que o upload e aceito (202) e a **extracao** falha, em estado `falhou` com o
        motivo. Continua sendo erro do cliente e nao do servico, e continua nao virando 500 — mas
        chega depois.

        O nome do teste mudou junto: chamar de "vira 422" um teste que afirma `falhou` seria
        mentir no lugar onde alguem vai procurar o comportamento.
        """
        corpo = await enviar_e_processar(client_doc, analise_id, conteudo=b"nao-e-uma-imagem")

        assert corpo["estado"] == "falhou"
        assert corpo["terminal"] is True
        assert "ErroLeituraDocumento" in corpo["erro"]

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
    async def test_confianca_intermediaria_exige_revisao(
        self,
        settings_teste: Settings,
        repositorio: RepositorioAnalisesMemoria,
        payload_analise: dict[str, Any],
        chaves_de_teste: Path,
    ) -> None:
        # POL-002 secao 3.2: entre 60% e 85% vai para conferencia humana.
        with montar_client(settings_teste, repositorio, chaves_de_teste, confianca=70) as client:
            aid = client.post("/v1/analises", json=payload_analise).json()["id"]
            corpo = await enviar_e_processar(client, aid)

        assert corpo["estado"] == "extraido"
        assert corpo["exige_revisao_humana"] is True

    async def test_confianca_baixa_e_rejeitada_com_instrucao(
        self,
        settings_teste: Settings,
        repositorio: RepositorioAnalisesMemoria,
        payload_analise: dict[str, Any],
        chaves_de_teste: Path,
    ) -> None:
        """Abaixo de 60% a POL-002 manda reenviar, e a instrucao continua chegando integral.

        ## A troca que a assincronia impos

        Era **422 na requisicao**: o cliente recebia a recusa junto com a instrucao, no mesmo
        ciclo. Agora ele recebeu 202 antes de a extracao rodar, e a rejeicao vira estado terminal.

        A informacao e a mesma — DPI minimo, referencia a politica —, mas chega depois e sob
        consulta. E pior para quem integra, e esta assumido como custo da camada. O que o teste
        garante e que ela nao virou silencio nem uma mensagem generica de erro: sem o DPI e sem a
        POL-002, o cliente nao saberia que a acao dele e reenviar com mais resolucao.
        """
        with montar_client(settings_teste, repositorio, chaves_de_teste, confianca=45) as client:
            aid = client.post("/v1/analises", json=payload_analise).json()["id"]
            corpo = await enviar_e_processar(client, aid)

        assert corpo["estado"] == "rejeitado"
        assert corpo["terminal"] is True
        assert "200 DPI" in corpo["erro"]
        assert "POL-002" in corpo["erro"]

    async def test_renda_ausente_exige_revisao(
        self,
        settings_teste: Settings,
        repositorio: RepositorioAnalisesMemoria,
        payload_analise: dict[str, Any],
        chaves_de_teste: Path,
    ) -> None:
        with montar_client(
            settings_teste,
            repositorio,
            chaves_de_teste,
            texto_ocr="RECIBO\nNome: JOAO DA SILVA\n",
        ) as client:
            aid = client.post("/v1/analises", json=payload_analise).json()["id"]
            corpo = await enviar_e_processar(client, aid)

        assert corpo["renda_comprovada"] is None
        assert corpo["exige_revisao_humana"] is True


class TestInjecaoDePrompt:
    """O ataque que a Camada 3 existe para conter.

    Um holerite visualmente legitimo com uma instrucao escondida no rodape,
    tentando fazer o sistema declarar uma renda inexistente.
    """

    async def test_documento_com_injecao_e_marcado(
        self,
        settings_teste: Settings,
        repositorio: RepositorioAnalisesMemoria,
        payload_analise: dict[str, Any],
        chaves_de_teste: Path,
    ) -> None:
        texto_atacado = HOLERITE_TEXTO + "\n" + INJECAO_TIPICA
        with montar_client(
            settings_teste, repositorio, chaves_de_teste, texto_ocr=texto_atacado
        ) as client:
            aid = client.post("/v1/analises", json=payload_analise).json()["id"]
            corpo = await enviar_e_processar(client, aid)

        assert corpo["injecao_suspeita"] is True
        assert len(corpo["categorias_injecao"]) >= 2

    async def test_injecao_forca_revisao_humana(
        self,
        settings_teste: Settings,
        repositorio: RepositorioAnalisesMemoria,
        payload_analise: dict[str, Any],
        chaves_de_teste: Path,
    ) -> None:
        texto_atacado = HOLERITE_TEXTO + "\n" + INJECAO_TIPICA
        with montar_client(
            settings_teste, repositorio, chaves_de_teste, texto_ocr=texto_atacado
        ) as client:
            aid = client.post("/v1/analises", json=payload_analise).json()["id"]
            corpo = await enviar_e_processar(client, aid)

        # Mesmo com confianca alta e todos os campos extraidos, o caso nao segue
        # automatico: tentativa de injecao em documento de credito e indicio de
        # fraude.
        # `qualidade` saiu da resposta (era funcao da confianca). O que importa aqui e que a
        # confianca esta **alta** e o caso vai para revisao de qualquer forma: injecao em
        # documento de credito e indicio de fraude, independente da leitura ter sido boa.
        assert Decimal(corpo["confianca_ocr_pct"]) >= 80
        assert corpo["exige_revisao_humana"] is True

    async def test_injecao_nao_altera_a_renda_apurada(
        self,
        settings_teste: Settings,
        repositorio: RepositorioAnalisesMemoria,
        payload_analise: dict[str, Any],
        chaves_de_teste: Path,
    ) -> None:
        """A defesa arquitetural, e nao textual.

        A injecao pede renda de R$ 50.000. O valor que alimenta o score vem da
        extracao por regex sobre o proprio documento, entao a instrucao nao tem
        por onde influenciar o numero — independentemente do que um LLM faria
        com aquele texto.
        """
        texto_atacado = HOLERITE_TEXTO + "\n" + INJECAO_TIPICA
        with montar_client(
            settings_teste, repositorio, chaves_de_teste, texto_ocr=texto_atacado
        ) as client:
            aid = client.post("/v1/analises", json=payload_analise).json()["id"]
            corpo = await enviar_e_processar(client, aid)

        assert corpo["renda_comprovada"] == "7262.14"
        assert corpo["renda_comprovada"] != "50000.00"

    async def test_documento_renderizado_com_injecao_e_contido(
        self,
        settings_teste: Settings,
        repositorio: RepositorioAnalisesMemoria,
        payload_analise: dict[str, Any],
        chaves_de_teste: Path,
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
        with montar_client(
            settings_teste, repositorio, chaves_de_teste, texto_ocr=texto_que_o_ocr_leria
        ) as client:
            aid = client.post("/v1/analises", json=payload_analise).json()["id"]
            corpo = await enviar_e_processar(client, aid, conteudo=buffer.getvalue())

        assert corpo["injecao_suspeita"] is True
        assert corpo["renda_comprovada"] == "7262.14"
        assert corpo["exige_revisao_humana"] is True
