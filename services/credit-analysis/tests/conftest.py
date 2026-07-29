"""Fixtures compartilhadas.

Os builders (`fazer_solicitante`, `fazer_proposta`) existem para que cada teste
declare apenas o que e relevante para ele. Um teste sobre comprometimento de
renda nao deveria precisar escolher uma data de nascimento — quando precisa, o
ruido esconde a intencao do teste.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from credit_analysis.api.app import criar_app
from credit_analysis.config import Ambiente, ProvedorLLM, Settings
from credit_analysis.domain.entities import PropostaCredito, Solicitante
from credit_analysis.domain.value_objects import CPF, Dinheiro, Percentual
from credit_analysis.infrastructure.bureau import BureauSempreLimpo, BureauSempreRestrito
from credit_analysis.infrastructure.event_loop import ajustar_policy_global
from credit_analysis.infrastructure.repositories.memoria import RepositorioAnalisesMemoria

# Ajustado no import do conftest, antes de o pytest-asyncio criar qualquer
# loop. Sobrescrever o fixture `event_loop_policy` faria o mesmo, mas esta
# depreciado; o hook que o substitui exige marcar cada teste individualmente.
ajustar_policy_global()

# CPFs sinteticos com digitos verificadores validos, usados so em teste.
CPF_VALIDO = "52998224725"
CPF_VALIDO_2 = "11144477735"


def fazer_solicitante(
    nome: str = "Maria Oliveira Santos",
    cpf: str = CPF_VALIDO,
    idade: int = 35,
    renda: str = "8500.00",
) -> Solicitante:
    nascimento = datetime.now(UTC).replace(year=datetime.now(UTC).year - idade)
    return Solicitante(
        nome=nome,
        cpf=CPF(cpf),
        data_nascimento=nascimento,
        renda_mensal_declarada=Dinheiro.de(renda),
    )


def fazer_proposta(
    valor: str = "45000.00",
    prazo: int = 36,
    taxa: str = "1.99",
) -> PropostaCredito:
    return PropostaCredito(
        valor_solicitado=Dinheiro.de(valor),
        prazo_meses=prazo,
        taxa_juros_mensal=Percentual.de(taxa),
    )


@pytest.fixture
def solicitante() -> Solicitante:
    return fazer_solicitante()


@pytest.fixture
def proposta() -> PropostaCredito:
    return fazer_proposta()


@pytest.fixture
def repositorio() -> RepositorioAnalisesMemoria:
    return RepositorioAnalisesMemoria()


@pytest.fixture
def settings_teste() -> Settings:
    """Configuracao explicita, imune ao ambiente.

    `postgres_dsn` e `anthropic_api_key` sao zerados de proposito: argumentos
    do construtor tem precedencia sobre variavel de ambiente no
    pydantic-settings, entao um `CREDIT_POSTGRES_DSN` exportado no shell de
    quem roda a suite nao muda o comportamento dos testes.

    Sem isso, rodar `pytest` depois de exportar o DSN fazia a API subir com
    pgvector de verdade — carregando o modelo de embedding de 2,24GB e
    quebrando os testes que verificam a degradacao sem indice. Teste que muda
    de resultado conforme o shell nao serve como rede de seguranca.
    """
    return Settings(
        ambiente=Ambiente.LOCAL,
        nivel_log="WARNING",  # silencia log de info durante os testes
        log_json=False,
        postgres_dsn="",
        anthropic_api_key="",
        # Explicito, e nao `auto`: com o Ollama instalado na maquina, o modo
        # automatico faria a suite chamar um modelo local de verdade — dezenas
        # de segundos por teste e resultado nao deterministico. Teste que muda
        # conforme o que esta instalado na maquina nao serve como rede de
        # seguranca.
        provedor_llm=ProvedorLLM.FAKE,
    )


@pytest.fixture
def client(
    settings_teste: Settings, repositorio: RepositorioAnalisesMemoria
) -> Iterator[TestClient]:
    """Cliente HTTP com bureau limpo — isola o efeito do score."""
    app = criar_app(settings=settings_teste, repositorio=repositorio, bureau=BureauSempreLimpo())
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client_restrito(
    settings_teste: Settings, repositorio: RepositorioAnalisesMemoria
) -> Iterator[TestClient]:
    """Cliente cujo bureau sempre acusa restricao — exercita o veto duro."""
    app = criar_app(settings=settings_teste, repositorio=repositorio, bureau=BureauSempreRestrito())
    with TestClient(app) as c:
        yield c


@pytest.fixture
def payload_analise() -> dict[str, object]:
    return {
        "solicitante": {
            "nome": "Maria Oliveira Santos",
            "cpf": "529.982.247-25",
            "data_nascimento": "1990-05-14T00:00:00Z",
            "renda_mensal_declarada": "8500.00",
        },
        "proposta": {
            "valor_solicitado": "45000.00",
            "prazo_meses": 36,
            "taxa_juros_mensal": "1.99",
        },
        "renda_comprovada": "8200.00",
        "meses_historico_bancario": 24,
    }


@pytest.fixture
def decimal_zero() -> Decimal:
    return Decimal("0")
