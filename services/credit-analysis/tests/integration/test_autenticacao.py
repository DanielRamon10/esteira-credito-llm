"""Autenticacao e autorizacao na borda HTTP.

## Por que este arquivo existe separado

O `client` do `conftest` carrega token com **todos** os escopos, de proposito: sem isso, cada
mudanca de escopo quebraria dezenas de testes que nao tratam de autorizacao, e o reflexo
seria afrouxar o escopo. O custo e que nenhum daqueles testes prova autorizacao.

Aqui os tokens sao restritos de proposito. E o teste mais importante nao e nenhum caso
especifico: e `test_toda_rota_de_negocio_exige_credencial`, que enumera o OpenAPI e pega a
proxima rota que nascer sem escopo — o modo de falha real, porque adicionar rota e rotina e
lembrar do `dependencies=[...]` nao e.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from plataforma import autenticacao as auth
from plataforma import emissor_local

from credit_analysis.api import seguranca
from credit_analysis.api.app import criar_app
from credit_analysis.api.seguranca import montar_chaveiro
from credit_analysis.config import Ambiente, ProvedorLLM, Settings
from credit_analysis.infrastructure.bureau import BureauSempreLimpo
from credit_analysis.infrastructure.repositories.memoria import RepositorioAnalisesMemoria
from tests.conftest import emitir_token, montar_cliente

pytestmark = pytest.mark.integration


@pytest.fixture
def app_de_teste(settings_teste: Settings) -> Any:
    return criar_app(
        settings=settings_teste,
        repositorio=RepositorioAnalisesMemoria(),
        bureau=BureauSempreLimpo(),
    )


@pytest.fixture
def sem_credencial(app_de_teste: Any) -> Iterator[TestClient]:
    """Cliente sem cabecalho `Authorization`."""
    with TestClient(app_de_teste) as c:
        yield c


class TestSemCredencial:
    def test_devolve_401_e_nao_403(self, sem_credencial: TestClient) -> None:
        """A distincao que um cliente automatizado usa para decidir o que fazer.

        401 manda apresentar credencial; 403 manda desistir. Trocados, um cliente correto
        entra em laco de renovacao ou para de tentar quando deveria renovar.
        """
        resposta = sem_credencial.get("/v1/analises")

        assert resposta.status_code == 401

    def test_traz_www_authenticate(self, sem_credencial: TestClient) -> None:
        """RFC 6750 secao 3.

        Sem este cabecalho o cliente nao tem como descobrir **como** se autenticar, e
        bibliotecas HTTP que renovam token sozinhas nao disparam a renovacao — o sintoma e
        "o cliente parou de funcionar" sem erro que aponte para o servidor.
        """
        cabecalho = sem_credencial.get("/v1/analises").headers["WWW-Authenticate"]

        assert cabecalho.startswith("Bearer")
        assert 'realm="credit-analysis"' in cabecalho
        # Token **ausente** nao e token invalido: sem `error`, o cliente sabe que precisa
        # apresentar credencial em vez de trocar a que tem.
        assert "error=" not in cabecalho

    def test_token_invalido_traz_error_no_cabecalho(self, sem_credencial: TestClient) -> None:
        resposta = sem_credencial.get(
            "/v1/analises", headers={"Authorization": "Bearer nao.e.um.jwt"}
        )

        assert resposta.status_code == 401
        assert 'error="invalid_token"' in resposta.headers["WWW-Authenticate"]

    @pytest.mark.parametrize(
        "cabecalho",
        [
            "",
            "Bearer",
            "Bearer ",
            "Basic dXNlcjpzZW5oYQ==",
            "token-sem-esquema",
            "Bearer a.b",  # JWT truncado
        ],
    )
    def test_cabecalho_malformado_e_401_e_nao_500(
        self, sem_credencial: TestClient, cabecalho: str
    ) -> None:
        """Nenhuma forma de cabecalho quebrado pode virar erro do servidor.

        500 num caminho de autenticacao e pior que 401: revela que a entrada chegou a codigo
        que nao a esperava, e e o primeiro sinal que alguem sondando procura.
        """
        resposta = sem_credencial.get("/v1/analises", headers={"Authorization": cabecalho})

        assert resposta.status_code == 401

    def test_a_resposta_nao_revela_o_motivo(self, sem_credencial: TestClient) -> None:
        """Expirado, audiencia errada e assinatura invalida devolvem o mesmo corpo.

        Distinguir ajudaria quem esta integrando **e** quem esta sondando: "audiencia
        incorreta" confirma que existe outro servico com outra audiencia. O motivo exato vai
        para o log e para a metrica, onde quem opera o ve.
        """
        corpo = sem_credencial.get(
            "/v1/analises", headers={"Authorization": "Bearer nao.e.um.jwt"}
        ).json()

        assert corpo["codigo"] == "nao_autenticado"
        for vazamento in ("expirado", "audiencia", "assinatura", "emissor"):
            assert vazamento not in corpo["mensagem"].lower()


class TestTokenInvalido:
    def test_expirado(self, app_de_teste: Any, chaves_de_teste: Path) -> None:
        antigo = emitir_token(chaves_de_teste, validade_segundos=60, agora=int(time.time()) - 3600)
        with montar_cliente(app_de_teste, antigo) as c:
            assert c.get("/v1/analises").status_code == 401

    def test_audiencia_de_outro_servico(self, app_de_teste: Any, chaves_de_teste: Path) -> None:
        """A escalada lateral que este monorepo tem tres oportunidades de sofrer.

        Um token do `customer-support` — o servico que fala com o publico — consultando
        analise de credito. A NetworkPolicy fecha o caminho de rede; a validacao de `aud`
        fecha o caminho da credencial. As duas defesas existem porque nenhuma das duas cobre
        o caso da outra: rede nao impede um cliente legitimo de reusar token, e `aud` nao
        impede um pod comprometido de falar com quem quiser.
        """
        do_suporte = emitir_token(chaves_de_teste, audiencia="customer-support")
        with montar_cliente(app_de_teste, do_suporte) as c:
            assert c.get("/v1/analises").status_code == 401

    def test_assinado_por_outra_chave(self, app_de_teste: Any, tmp_path: Path) -> None:
        outras = emissor_local.gerar_chaves(tmp_path / "outras")
        forjado = emitir_token(outras)
        with montar_cliente(app_de_teste, forjado) as c:
            assert c.get("/v1/analises").status_code == 401

    def test_emissor_desconhecido(self, app_de_teste: Any, chaves_de_teste: Path) -> None:
        """Mesma chave, outro emissor: o caso de um IdP compartilhado na organizacao."""
        alheio = emitir_token(chaves_de_teste, emissor="https://outro-sistema.invalid")
        with montar_cliente(app_de_teste, alheio) as c:
            assert c.get("/v1/analises").status_code == 401


class TestEscopos:
    def test_escopo_ausente_e_403_e_nao_401(self, app_de_teste: Any, chaves_de_teste: Path) -> None:
        """Credencial valida, permissao ausente.

        Devolver 401 aqui manda um cliente correto reautenticar num laco que nunca resolve, e
        esconde de quem opera que o problema e de permissao e nao de credencial.
        """
        so_leitura = emitir_token(chaves_de_teste, escopos=[seguranca.ANALISES_LER])
        with montar_cliente(app_de_teste, so_leitura) as c:
            resposta = c.post("/v1/analises", json={})

        assert resposta.status_code == 403

    def test_403_nao_diz_qual_escopo_falta(self, app_de_teste: Any, chaves_de_teste: Path) -> None:
        """Enumerar escopos para quem nao os tem e entregar o mapa de permissoes.

        Mesma logica da fronteira de divulgacao do `customer-support`: a informacao util para
        quem integra e a mesma que serve para quem sonda, e a primeira tem outro canal (a
        documentacao, e o log de quem opera).
        """
        so_leitura = emitir_token(chaves_de_teste, escopos=[seguranca.ANALISES_LER])
        with montar_cliente(app_de_teste, so_leitura) as c:
            corpo = c.post("/v1/analises", json={}).json()

        assert corpo["codigo"] == "escopo_insuficiente"
        assert "analises:escrever" not in corpo["mensagem"]

    def test_token_sem_escopo_algum_nao_le_nada(
        self, app_de_teste: Any, chaves_de_teste: Path
    ) -> None:
        """Token valido e vazio: autenticado, sem autorizacao para coisa alguma."""
        vazio = emitir_token(chaves_de_teste, escopos=[])
        with montar_cliente(app_de_teste, vazio) as c:
            assert c.get("/v1/analises").status_code == 403

    def test_escopo_de_leitura_nao_da_escrita(
        self, app_de_teste: Any, chaves_de_teste: Path, payload_analise: dict[str, object]
    ) -> None:
        """A granularidade que um `credito:tudo` destruiria.

        O painel de analista le; o canal de originacao escreve. Com escopo unico, qualquer
        credencial vazada de um lado serve para o outro.
        """
        so_leitura = emitir_token(chaves_de_teste, escopos=[seguranca.ANALISES_LER])
        with montar_cliente(app_de_teste, so_leitura) as c:
            assert c.get("/v1/analises").status_code == 200
            assert c.post("/v1/analises", json=payload_analise).status_code == 403

    def test_escopo_de_documento_e_separado_do_de_escrita(
        self, app_de_teste: Any, chaves_de_teste: Path, payload_analise: dict[str, object]
    ) -> None:
        """Enviar documento e o unico caminho de conteudo nao confiavel neste servico.

        E a superficie de OCR e de prompt injection. Quem so registra proposta estruturada
        nao deveria poder abri-la — por isso `documentos:enviar` nao vem junto com
        `analises:escrever`.
        """
        escritor = emitir_token(
            chaves_de_teste, escopos=[seguranca.ANALISES_ESCREVER, seguranca.ANALISES_LER]
        )
        with montar_cliente(app_de_teste, escritor) as c:
            aid = c.post("/v1/analises", json=payload_analise).json()["id"]
            resposta = c.post(
                f"/v1/analises/{aid}/documentos",
                files={"arquivo": ("x.png", b"nao-importa", "image/png")},
                data={"tipo": "holerite"},
            )

        assert resposta.status_code == 403

    def test_escopo_desconhecido_falha_no_import_e_nao_na_requisicao(self) -> None:
        """Erro de digitacao no escopo devolveria 403 para todo mundo em producao.

        E o sintoma — "ninguem consegue acessar esta rota" — nao aponta para a causa. Falhar
        na construcao da aplicacao transforma isso num erro de subida, que o rollout detecta.
        """
        with pytest.raises(ValueError, match="escopo desconhecido"):
            seguranca.Escopo("analises:lerr")


class TestCoberturaDasRotas:
    """O teste estrutural: nenhuma rota de negocio sem credencial."""

    # Rotas deliberadamente **abertas**, com o motivo de cada uma. Lista fechada: qualquer
    # rota nova fora daqui precisa exigir token, e o teste abaixo falha se nao exigir.
    ABERTAS: ClassVar[set[str]] = {
        # Sondas do Kubernetes. O kubelet nao carrega credencial, e exigir token aqui
        # transformaria uma configuracao de auth errada em pod reiniciando em laco — o
        # servico ficaria indisponivel pela propria defesa.
        "/health",
        "/ready",
        # Scrape do Prometheus. Protegida pela NetworkPolicy (so o namespace
        # `observabilidade` alcanca a porta), e os testes de cardinalidade garantem que a
        # exposicao nao contem CPF, nome nem UUID. Exigir token aqui obrigaria a distribuir
        # credencial para o Prometheus, que e mais superficie que a que se fecha.
        "/metrics",
    }

    def test_toda_rota_de_negocio_exige_credencial(self, sem_credencial: TestClient) -> None:
        """Enumera o OpenAPI e confirma 401 sem token, rota por rota.

        Este e o teste que pega o modo de falha **real**: adicionar rota e rotina, e lembrar
        do `dependencies=[Depends(Escopo(...))]` nao e. Um teste por rota exigiria disciplina
        de quem cria a rota — exatamente a disciplina que falhou.

        Comportamental e nao por introspeccao: verificar se a dependencia esta na lista do
        objeto `APIRoute` provaria que **algo** foi declarado, nao que ele nega o acesso.
        """
        caminhos = sem_credencial.get("/openapi.json").json()["paths"]
        desprotegidas: list[str] = []

        for caminho, operacoes in caminhos.items():
            if caminho in self.ABERTAS:
                continue
            concreto = caminho.replace("{analise_id}", str(uuid4()))
            for metodo in operacoes:
                resposta = sem_credencial.request(metodo.upper(), concreto)
                if resposta.status_code != 401:
                    desprotegidas.append(f"{metodo.upper()} {caminho} -> {resposta.status_code}")

        assert not desprotegidas, f"rotas sem credencial exigida: {desprotegidas}"

    @pytest.mark.parametrize("caminho", sorted(ABERTAS))
    def test_rotas_abertas_continuam_abertas(
        self, sem_credencial: TestClient, caminho: str
    ) -> None:
        """A contrapartida, e ela importa tanto quanto.

        Sem este teste, alguem "fechando tudo" poria token no `/health` e o pod entraria em
        laco de reinicio na primeira configuracao errada de auth — a defesa causando a
        indisponibilidade que deveria evitar.
        """
        assert sem_credencial.get(caminho).status_code == 200

    def test_a_lista_de_abertas_nao_cresceu_sem_justificativa(self) -> None:
        """Guarda de tamanho: sao tres, e cada uma tem o motivo escrito acima.

        Uma quarta entrada exige mexer nesta linha, o que forca a decisao a passar por
        revisao em vez de entrar como detalhe.
        """
        assert len(self.ABERTAS) == 3


class TestObservabilidade:
    def test_aceite_e_negativa_aparecem_na_metrica(
        self, app_de_teste: Any, chaves_de_teste: Path
    ) -> None:
        """O aceito e contado junto com as negativas, e o denominador e o ponto.

        "50 negativas em 10 minutos" nao distingue um cliente recem-integrado com
        configuracao errada de forca bruta. O que separa os dois e a proporcao sobre o total.
        """
        with montar_cliente(app_de_teste, emitir_token(chaves_de_teste)) as c:
            c.get("/v1/analises")
            exposicao = c.get("/metrics").text

        assert 'credito_auth_decisoes_total{evento="aceito"' in exposicao

    def test_o_token_nunca_aparece_na_exposicao_de_metricas(
        self, app_de_teste: Any, chaves_de_teste: Path
    ) -> None:
        """Credencial em label seria cardinalidade ilimitada **e** vazamento.

        `/metrics` nao tem autenticacao dentro do cluster, e metrica vai para painel, alerta,
        e-mail e print de Slack — nenhum deles com controle de acesso a credencial.
        """
        token = emitir_token(chaves_de_teste)
        with montar_cliente(app_de_teste, token) as c:
            c.get("/v1/analises")
            exposicao = c.get("/metrics").text

        assert token not in exposicao
        # Nem um prefixo: o header de um JWT ja revela algoritmo, e o corpo revela emissor e
        # audiencia em base64 trivialmente decodificavel.
        assert token[:24] not in exposicao


class TestConfiguracao:
    def test_sem_chave_configurada_a_api_nao_sobe(self) -> None:
        """Nao ha modo desligado, e este teste e o que garante isso.

        As tres saidas possiveis para uma API sem chave de verificacao: recusar tudo
        (indisponivel), aceitar tudo (aberto), ou nao subir. Somente a terceira nao esconde o
        problema — e e a que o Kubernetes trata bem, mantendo os pods antigos no ar.

        **A garantia mudou de lugar na Camada 9, e nao de forca.** Antes era o validador de
        `Settings`; agora e `montar_chaveiro`, que `criar_app` chama no boot. O motivo esta na
        docstring de `_conferir_fonte_de_chave`: com a exigencia em `Settings`, o trabalhador de
        extracao — que consome fila e nao verifica token — tambem precisava de chave.

        Por isso o teste exercita `criar_app` e nao o construtor de `Settings`: e o boot da API que
        precisa falhar, e e ele que este teste mede.
        """
        settings = Settings(
            ambiente=Ambiente.LOCAL,
            provedor_llm=ProvedorLLM.FAKE,
            auth_chave_publica="",
            auth_jwks_url="",
            auth_emissor="https://local.invalid",
            _env_file=None,  # type: ignore[call-arg]
        )

        with pytest.raises(RuntimeError, match="CREDIT_AUTH_CHAVE_PUBLICA"):
            criar_app(settings=settings)

    def test_settings_sem_chave_e_valido_para_quem_nao_verifica_token(self) -> None:
        """A outra metade da mudanca, e a que faz o trabalhador poder subir.

        Sem este teste, alguem devolveria a exigencia para o validador de `Settings` — a mudanca
        parece uma frouxidao de seguranca quando lida isolada — e o unico sintoma seria o
        trabalhador recusando subir com uma mensagem sobre chave de auth que ele nunca usa.

        O par com `test_sem_chave_configurada_a_api_nao_sobe` e o que documenta a fronteira: a
        configuracao e valida, e servir HTTP com ela nao e.
        """
        settings = Settings(
            ambiente=Ambiente.LOCAL,
            provedor_llm=ProvedorLLM.FAKE,
            auth_chave_publica="",
            auth_jwks_url="",
            _env_file=None,  # type: ignore[call-arg]
        )

        assert settings.auth_chave_publica == ""
        assert settings.auth_jwks_url == ""

    def test_duas_fontes_de_chave_sao_recusadas(self, chave_publica_de_teste: str) -> None:
        """Ambiguidade sobre qual chave manda e como se aceita token que devia ser negado.

        O cenario real: migracao de PEM para JWKS em que alguem esquece de remover o PEM
        antigo. A configuracao "funciona", e ninguem sabe qual das duas esta validando.
        """
        with pytest.raises(ValueError, match=r"informe \*\*uma\*\* fonte"):
            Settings(
                ambiente=Ambiente.LOCAL,
                provedor_llm=ProvedorLLM.FAKE,
                auth_chave_publica=chave_publica_de_teste,
                auth_jwks_url="https://idp.invalid/jwks",
                auth_emissor="x",
                _env_file=None,  # type: ignore[call-arg]
            )

    def test_chave_privada_no_verificador_e_recusada(self, chaves_de_teste: Path) -> None:
        """Chave privada no resource server e a capacidade de **assinar**, nao de verificar.

        O erro e plausivel: quem configura tem as duas no mesmo diretorio.
        """
        privada = (chaves_de_teste / emissor_local.NOME_PRIVADA).read_text(encoding="utf-8")

        with pytest.raises(ValueError, match="PRIVADA"):
            auth.Chaveiro.de_chave_publica(privada)

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
                provedor_llm=ProvedorLLM.FAKE,
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
                    provedor_llm=ProvedorLLM.FAKE,
                )
            )
