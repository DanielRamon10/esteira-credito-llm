"""Testes da selecao de provedor de LLM.

A selecao automatica e conveniente e perigosa na mesma medida: cair no fake em
silencio faz um parecer determinístico parecer gerado por modelo. Estes testes
fixam as duas garantias — que o `auto` escolhe o melhor disponivel, e que um
provedor pedido explicitamente **falha em vez de degradar em silencio**.
"""

from __future__ import annotations

import pytest

from credit_analysis.api.app import _montar_llm
from credit_analysis.config import Ambiente, ProvedorLLM, Settings
from credit_analysis.infrastructure.llm.anthropic_adapter import LLMAnthropic, LLMFake
from credit_analysis.infrastructure.llm.ollama_adapter import LLMOllama

# De proposito **sem** o formato real de uma chave (`sk-ant-...`): o que o
# codigo testa e presenca, nao formato (`usar_llm_real` so verifica se a string
# nao esta vazia). Um valor com cara de chave de verdade faria o hook de
# pre-commit bloquear o commit — corretamente, ja que ele nao tem como saber
# que esta e falsa. Enfraquecer a varredura para acomodar um teste seria trocar
# uma defesa real por uma conveniencia.
CHAVE_FALSA = "credencial-sintetica-de-teste"


def settings(**kwargs: object) -> Settings:
    base: dict[str, object] = {
        "ambiente": Ambiente.LOCAL,
        "nivel_log": "ERROR",
        "log_json": False,
        "postgres_dsn": "",
        "anthropic_api_key": "",
    }
    base.update(kwargs)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.fixture
def sem_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("credit_analysis.api.app.ollama_disponivel", lambda *_, **__: False)


@pytest.fixture
def com_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("credit_analysis.api.app.ollama_disponivel", lambda *_, **__: True)
    monkeypatch.setattr(
        "credit_analysis.api.app.modelos_instalados", lambda *_, **__: ("llama3.1:8b",)
    )


class TestSelecaoAutomatica:
    def test_prefere_anthropic_quando_ha_chave(self, com_ollama: None) -> None:
        # Mesmo com Ollama disponivel: quem pagou pela chave quer usa-la.
        llm = _montar_llm(settings(anthropic_api_key=CHAVE_FALSA))
        assert isinstance(llm, LLMAnthropic)

    def test_usa_ollama_quando_nao_ha_chave(self, com_ollama: None) -> None:
        llm = _montar_llm(settings())
        assert isinstance(llm, LLMOllama)
        assert llm.identificacao.startswith("ollama:")

    def test_cai_no_fake_sem_nenhum_provedor(self, sem_ollama: None) -> None:
        llm = _montar_llm(settings())
        assert isinstance(llm, LLMFake)

    def test_avisa_quando_o_modelo_nao_esta_baixado(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Devolver erro do daemon no meio de uma requisicao de negocio seria
        # pior que avisar na subida.
        monkeypatch.setattr("credit_analysis.api.app.ollama_disponivel", lambda *_, **__: True)
        monkeypatch.setattr(
            "credit_analysis.api.app.modelos_instalados", lambda *_, **__: ("outro:7b",)
        )
        llm = _montar_llm(settings(modelo_ollama="llama3.1:8b"))
        assert isinstance(llm, LLMOllama)  # segue, mas com warning no log


class TestSelecaoExplicita:
    def test_fake_forcado(self, com_ollama: None) -> None:
        llm = _montar_llm(settings(provedor_llm=ProvedorLLM.FAKE, anthropic_api_key=CHAVE_FALSA))
        assert isinstance(llm, LLMFake)

    def test_anthropic_sem_chave_falha_em_vez_de_degradar(self, com_ollama: None) -> None:
        # Degradar em silencio faria alguem descobrir na revisao do parecer que
        # o texto veio de um fake.
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            _montar_llm(settings(provedor_llm=ProvedorLLM.ANTHROPIC))

    def test_ollama_indisponivel_falha_com_instrucao(self, sem_ollama: None) -> None:
        with pytest.raises(RuntimeError, match="ollama serve"):
            _montar_llm(settings(provedor_llm=ProvedorLLM.OLLAMA))

    def test_ollama_forcado_ignora_a_chave_anthropic(self, com_ollama: None) -> None:
        llm = _montar_llm(settings(provedor_llm=ProvedorLLM.OLLAMA, anthropic_api_key=CHAVE_FALSA))
        assert isinstance(llm, LLMOllama)


class TestAdapterOllama:
    def test_identificacao_inclui_o_modelo(self) -> None:
        # O parecer registra qual modelo redigiu; auditoria precisa disso.
        assert LLMOllama(modelo="llama3.2:3b").identificacao == "ollama:llama3.2:3b"

    def test_respeita_o_port(self) -> None:
        from credit_analysis.application.ports import ModeloLinguagem

        assert isinstance(LLMOllama(), ModeloLinguagem)

    def test_nao_conecta_na_construcao(self) -> None:
        # Construtor nao pode fazer I/O: a app precisa subir mesmo com o
        # daemon fora do ar, e falhar so quando alguem pedir uma geracao.
        LLMOllama(endpoint="http://127.0.0.1:1")  # porta invalida, sem erro
