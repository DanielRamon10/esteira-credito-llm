"""Autenticacao e autorizacao na borda HTTP.

O teste que mais importa aqui nao e nenhum caso especifico: e
`test_toda_rota_de_negocio_exige_credencial`, que enumera o OpenAPI e pega a proxima rota que
nascer sem escopo. Adicionar rota e rotina; lembrar do `dependencies=[...]` nao e.

O segundo mais importante e `test_executar_nao_da_direito_de_listar`. Ele protege a razao de
os dois escopos existirem: quem executa triagem e a esteira de credito, no meio de uma
analise; quem le triagem passada e o time de conformidade, respondendo auditoria. Dar leitura
a esteira daria a ela a lista de pessoas em situacao sensivel — que ela nao precisa.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from plataforma import emissor_local

from kyc_compliance.api import seguranca
from kyc_compliance.api.app import criar_app
from kyc_compliance.api.seguranca import montar_chaveiro
from kyc_compliance.config import Ambiente, Settings
from kyc_compliance.domain.triagem import EntradaRestritiva
from kyc_compliance.infrastructure.listas import ListasEmMemoria
from tests.conftest import CPF_LIMPO, emitir_token, montar_cliente

pytestmark = pytest.mark.integration

PAYLOAD = {"nome": "Beatriz Nogueira Prado", "cpf": CPF_LIMPO}


@pytest.fixture
def app_de_teste(settings_teste: Settings, entradas: list[EntradaRestritiva]) -> Any:
    return criar_app(settings=settings_teste, listas=ListasEmMemoria(entradas, "teste"))


@pytest.fixture
def sem_credencial(app_de_teste: Any) -> Iterator[TestClient]:
    with TestClient(app_de_teste) as c:
        yield c


class TestSemCredencial:
    def test_401_com_www_authenticate(self, sem_credencial: TestClient) -> None:
        """RFC 6750 secao 3: sem o cabecalho, cliente que renova token sozinho nao renova."""
        resposta = sem_credencial.post("/v1/triagens", json=PAYLOAD)

        assert resposta.status_code == 401
        assert 'realm="kyc-compliance"' in resposta.headers["WWW-Authenticate"]

    @pytest.mark.parametrize(
        "cabecalho", ["", "Bearer", "Basic dXNlcjpzZW5oYQ==", "abc", "Bearer a.b"]
    )
    def test_cabecalho_malformado_e_401_e_nao_500(
        self, sem_credencial: TestClient, cabecalho: str
    ) -> None:
        """500 num caminho de autenticacao revela que a entrada chegou a codigo que nao a
        esperava, e e o primeiro sinal que alguem sondando procura."""
        resposta = sem_credencial.post(
            "/v1/triagens", json=PAYLOAD, headers={"Authorization": cabecalho}
        )

        assert resposta.status_code == 401

    def test_a_resposta_nao_revela_o_motivo(self, sem_credencial: TestClient) -> None:
        corpo = sem_credencial.post(
            "/v1/triagens", json=PAYLOAD, headers={"Authorization": "Bearer x.y.z"}
        ).json()

        assert corpo["codigo"] == "nao_autenticado"
        for vazamento in ("expirado", "audiencia", "assinatura", "emissor"):
            assert vazamento not in corpo["mensagem"].lower()


class TestTokenInvalido:
    def test_expirado(self, app_de_teste: Any) -> None:
        antigo = emitir_token(validade_segundos=60, agora=int(time.time()) - 3600)
        with montar_cliente(app_de_teste, antigo) as c:
            assert c.post("/v1/triagens", json=PAYLOAD).status_code == 401

    def test_token_da_esteira_de_credito_nao_serve(self, app_de_teste: Any) -> None:
        """A escalada lateral que a validacao de `aud` fecha.

        Um token emitido para o `credit-analysis` nao vale aqui, mesmo vindo do consumidor
        legitimo. A NetworkPolicy so permite o pod certo alcancar a porta; o `aud` garante que
        a credencial tambem foi emitida para este servico. Rede nao impede reuso de token, e
        `aud` nao impede pod comprometido — as duas defesas cobrem casos diferentes.
        """
        da_esteira = emitir_token(audiencia="credit-analysis")
        with montar_cliente(app_de_teste, da_esteira) as c:
            assert c.post("/v1/triagens", json=PAYLOAD).status_code == 401

    def test_assinado_por_outra_chave(self, app_de_teste: Any, tmp_path: Path) -> None:
        outras = emissor_local.gerar_chaves(tmp_path / "outras")
        with montar_cliente(app_de_teste, emitir_token(outras)) as c:
            assert c.post("/v1/triagens", json=PAYLOAD).status_code == 401


class TestEscopos:
    def test_executar_nao_da_direito_de_listar(self, app_de_teste: Any) -> None:
        """A separacao que existe por causa de **quem** consome cada operacao.

        A esteira de credito executa triagem sincronamente e nao tem motivo para varrer o
        historico — que e a lista de quem foi triado, ou seja de pessoas em situacao sensivel.
        Com um escopo unico, um bug na esteira ou um vazamento da credencial dela entregaria
        essa lista.
        """
        so_executa = emitir_token(escopos=[seguranca.TRIAGENS_EXECUTAR])
        with montar_cliente(app_de_teste, so_executa) as c:
            assert c.post("/v1/triagens", json=PAYLOAD).status_code == 201
            assert c.get("/v1/triagens").status_code == 403

    def test_ler_nao_da_direito_de_executar(self, app_de_teste: Any) -> None:
        """A contrapartida: conformidade audita, nao origina triagem."""
        so_le = emitir_token(escopos=[seguranca.TRIAGENS_LER])
        with montar_cliente(app_de_teste, so_le) as c:
            assert c.get("/v1/triagens").status_code == 200
            assert c.post("/v1/triagens", json=PAYLOAD).status_code == 403

    def test_403_nao_diz_qual_escopo_falta(self, app_de_teste: Any) -> None:
        so_le = emitir_token(escopos=[seguranca.TRIAGENS_LER])
        with montar_cliente(app_de_teste, so_le) as c:
            corpo = c.post("/v1/triagens", json=PAYLOAD).json()

        assert corpo["codigo"] == "escopo_insuficiente"
        assert "triagens:executar" not in corpo["mensagem"]

    def test_escopo_desconhecido_falha_na_construcao(self) -> None:
        with pytest.raises(ValueError, match="escopo desconhecido"):
            seguranca.Escopo("triagens:executarr")


class TestCoberturaDasRotas:
    # Rotas deliberadamente abertas. `/health` e `/ready` porque o kubelet nao carrega
    # credencial — exigir token ali transformaria configuracao de auth errada em pod
    # reiniciando em laco. `/metrics` porque o Prometheus a raspa, e a NetworkPolicy ja limita
    # quem alcanca a porta; os testes de cardinalidade garantem que a exposicao nao contem
    # nome nem CPF.
    ABERTAS: ClassVar[set[str]] = {"/health", "/ready", "/metrics"}

    def test_toda_rota_de_negocio_exige_credencial(self, sem_credencial: TestClient) -> None:
        """Comportamental e nao por introspeccao: conferir a lista de dependencias do
        `APIRoute` provaria que algo foi declarado, nao que ele nega acesso."""
        caminhos = sem_credencial.get("/openapi.json").json()["paths"]
        desprotegidas: list[str] = []

        for caminho, operacoes in caminhos.items():
            if caminho in self.ABERTAS:
                continue
            concreto = caminho.replace("{triagem_id}", str(uuid4()))
            for metodo in operacoes:
                resposta = sem_credencial.request(metodo.upper(), concreto)
                if resposta.status_code != 401:
                    desprotegidas.append(f"{metodo.upper()} {caminho} -> {resposta.status_code}")

        assert not desprotegidas, f"rotas sem credencial exigida: {desprotegidas}"

    @pytest.mark.parametrize("caminho", sorted(ABERTAS))
    def test_rotas_abertas_continuam_abertas(
        self, sem_credencial: TestClient, caminho: str
    ) -> None:
        """Sem este teste, alguem "fechando tudo" poria token no `/health` e o pod entraria em
        laco de reinicio na primeira configuracao errada de auth."""
        assert sem_credencial.get(caminho).status_code == 200

    def test_a_lista_de_abertas_nao_cresceu(self) -> None:
        assert len(self.ABERTAS) == 3


class TestObservabilidade:
    def test_aceite_e_negativa_aparecem_na_metrica(self, app_de_teste: Any) -> None:
        """O aceito e contado junto: sem denominador, "50 negativas" nao distingue cliente mal
        configurado de forca bruta."""
        with montar_cliente(app_de_teste) as c:
            c.get("/v1/triagens")
            exposicao = c.get("/metrics").text

        assert 'kyc_auth_decisoes_total{evento="aceito"' in exposicao

    def test_o_token_nunca_aparece_na_exposicao(self, app_de_teste: Any) -> None:
        token = emitir_token()
        with montar_cliente(app_de_teste, token) as c:
            c.get("/v1/triagens")
            exposicao = c.get("/metrics").text

        assert token not in exposicao
        assert token[:24] not in exposicao


class TestConfiguracao:
    def test_sem_chave_o_servico_nao_sobe(self) -> None:
        """As tres saidas para um servico sem chave: recusar tudo (indisponivel), aceitar tudo
        (aberto), ou nao subir. Somente a terceira nao esconde o problema."""
        with pytest.raises(ValueError, match="KYC_AUTH_CHAVE_PUBLICA"):
            Settings(
                ambiente=Ambiente.LOCAL,
                auth_chave_publica="",
                auth_jwks_url="",
                auth_emissor="x",
                _env_file=None,  # type: ignore[call-arg]
            )

    def test_duas_fontes_de_chave_sao_recusadas(self, chave_publica_de_teste: str) -> None:
        with pytest.raises(ValueError, match=r"informe \*\*uma\*\* fonte"):
            Settings(
                ambiente=Ambiente.LOCAL,
                auth_chave_publica=chave_publica_de_teste,
                auth_jwks_url="https://idp.invalid/jwks",
                auth_emissor="x",
                _env_file=None,  # type: ignore[call-arg]
            )

    def test_chave_por_arquivo_e_a_forma_usada_por_compose_e_k8s(
        self, chaves_de_teste: Path
    ) -> None:
        """A terceira fonte, e ela existe por dois motivos concretos.

        PEM em variavel de ambiente aparece inteiro num `kubectl describe pod` e num
        `docker inspect`; e variavel de ambiente e fixada na criacao do container, enquanto um
        Secret montado como volume o kubelet atualiza em segundos — ou seja, arquivo permite
        rotacao sem recriar o pod, e variavel nao.
        """
        montar_chaveiro(
            Settings(
                # `auth_chave_publica=""` explicito: o conftest exporta a variavel de ambiente, e
                # `_env_file=None` desliga o `.env` mas **nao** o ambiente. Sem zerar aqui haveria
                # duas fontes, e o validador recusaria — o teste falharia pelo motivo errado.
                auth_chave_publica="",
                auth_chave_publica_arquivo=chaves_de_teste / "publica.pem",
                auth_emissor="x",
                _env_file=None,  # type: ignore[call-arg]
            )
        )

    def test_arquivo_ausente_e_erro_de_subida_com_o_caminho(self, tmp_path: Path) -> None:
        """`FileNotFoundError: publica.pem` num container nao diz se o volume nao foi montado
        ou se o nome do arquivo esta errado. A mensagem traz o caminho resolvido."""
        with pytest.raises(RuntimeError, match="chave publica ausente"):
            montar_chaveiro(
                Settings(
                    # `auth_chave_publica=""` explicito: o conftest exporta a variavel de
                    # ambiente, e `_env_file=None` desliga o `.env` mas **nao** o ambiente. Sem
                    # zerar aqui, haveria duas fontes e o validador recusaria — o teste falharia
                    # com a mensagem certa pelo motivo errado.
                    auth_chave_publica="",
                    auth_chave_publica_arquivo=tmp_path / "nao-existe.pem",
                    auth_emissor="x",
                    _env_file=None,  # type: ignore[call-arg]
                )
            )
