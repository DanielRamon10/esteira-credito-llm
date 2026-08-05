"""Fixtures compartilhadas.

Os builders (`fazer_solicitante`, `fazer_proposta`) existem para que cada teste
declare apenas o que e relevante para ele. Um teste sobre comprometimento de
renda nao deveria precisar escolher uma data de nascimento — quando precisa, o
ruido esconde a intencao do teste.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from plataforma import emissor_local

from credit_analysis.api import seguranca
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


# --------------------------------------------------------- Autenticacao (C7)
#
# As chaves sao geradas **no import** deste arquivo, e nao numa fixture. Fixture nao serve
# aqui: `test_pgvector.py` chama `get_settings()` no proprio guard de skip, ou seja durante a
# **coleta**, antes de qualquer fixture existir. Como autenticacao nao tem modo desligado, um
# `Settings()` sem chave configurada levanta — e a suite inteira falhava no import do
# conftest, com uma mensagem sobre autenticacao num arquivo que trata de pgvector.
#
# O par vive num diretorio temporario e **nunca** aparece literal aqui: o
# `.githooks/pre-commit` bloqueia PEM, e a saida certa e o teste nao ter o padrao — nao
# enfraquecer o scanner, reflexo que este projeto ja teve duas vezes por outros motivos.
_DIRETORIO_DE_CHAVES = Path(tempfile.mkdtemp(prefix="credit-chaves-"))
emissor_local.gerar_chaves(_DIRETORIO_DE_CHAVES)
CHAVE_PUBLICA_DE_TESTE = emissor_local.chave_publica(_DIRETORIO_DE_CHAVES)

# Tambem no ambiente, e nao apenas no `Settings` construido pelas fixtures: os caminhos que
# leem a configuracao do processo (`get_settings`, `ingestao`, `__main__`) nao passam pelas
# fixtures. E o que producao faz — a chave chega por variavel de ambiente.
os.environ.setdefault("CREDIT_AUTH_CHAVE_PUBLICA", CHAVE_PUBLICA_DE_TESTE)
os.environ.setdefault("CREDIT_AUTH_EMISSOR", emissor_local.EMISSOR_LOCAL)

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
def settings_teste(chave_publica_de_teste: str) -> Settings:
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
        # Autenticacao NAO tem modo desligado (ver api/seguranca.py), entao a suite precisa
        # de uma chave de verificacao real. E a chave da sessao, nao um valor fixo: chave
        # literal no repositorio e chave publicada.
        auth_chave_publica=chave_publica_de_teste,
        auth_emissor=emissor_local.EMISSOR_LOCAL,
    )


# --------------------------------------------------------- Autenticacao (C7)
#
# O par de chaves e gerado uma vez por sessao, em `tmp_path_factory`, e **nunca** aparece
# literal neste arquivo: o `.githooks/pre-commit` bloqueia PEM, e a saida certa e o teste
# nao ter o padrao — nao enfraquecer o scanner, o que ja foi tentado duas vezes neste
# projeto por outros motivos.


@pytest.fixture(scope="session")
def chaves_de_teste() -> Path:
    """Diretorio das chaves geradas no import. Gerar RSA custa ~100ms; uma vez por sessao."""
    return _DIRETORIO_DE_CHAVES


@pytest.fixture(scope="session")
def chave_publica_de_teste() -> str:
    return CHAVE_PUBLICA_DE_TESTE


def emitir_token(
    chaves: Path,
    *,
    escopos: Sequence[str] = seguranca.TODOS_OS_ESCOPOS,
    audiencia: str = "credit-analysis",
    # Parametros explicitos e nao `**kwargs`: com `**kwargs: object` o `--strict` recusa o
    # repasse, e a saida facil seria `Any` — perder tipagem num helper que constroi credencial
    # e onde menos se quer.
    emissor: str = emissor_local.EMISSOR_LOCAL,
    validade_segundos: int = emissor_local.VALIDADE_PADRAO_SEGUNDOS,
    agora: int | None = None,
    locatario: str | None = None,
) -> str:
    """Token de teste. O default carrega **todos** os escopos.

    Isso e conveniencia deliberada, com um custo assumido: nenhum dos testes existentes
    prova autorizacao, porque todos passam com credencial total. O que cobre autorizacao e
    `tests/integration/test_autenticacao.py`, que emite token restrito de proposito — e o
    teste que enumera as rotas do OpenAPI, que pega a proxima rota criada sem escopo.

    A alternativa — dar a cada teste o escopo minimo — faria cada mudanca de escopo quebrar
    dezenas de testes que nao tratam de autorizacao, e o reflexo seria afrouxar o escopo.
    """
    return emissor_local.emitir(
        audiencia=audiencia,
        escopos=list(escopos),
        diretorio=chaves,
        emissor=emissor,
        validade_segundos=validade_segundos,
        agora=agora,
        locatario=locatario,
    )


def montar_cliente(app: FastAPI, token: str) -> TestClient:
    """`TestClient` que se comporta como um cliente bem-educado: token e chave de idempotencia.

    O `Authorization` vai no construtor e nao em cada chamada: com 9 clientes e centenas de
    requisicoes na suite, passar por chamada garantiria que alguma ficasse sem — e o teste
    falharia com 401 por esquecimento, o que treina a ler 401 como ruido.

    ## `Idempotency-Key` por gancho, e nao no construtor

    A Camada 11 tornou a chave obrigatoria em `POST /v1/analises`. Poe-la no construtor seria o
    caminho curto e estaria **errado**: o cabecalho ficaria fixo, e duas criacoes no mesmo teste
    compartilhariam a chave — a segunda receberia a analise da primeira, e o teste mediria
    idempotencia onde queria medir outra coisa.

    O gancho gera uma chave nova por requisicao, que e o que um cliente correto faz. Ele
    **respeita** a chave ja presente: os testes de idempotencia mandam a delas e precisam que ela
    chegue.

    ## O que este gancho esconde, e o teste que cobre o buraco

    Com ele, nenhuma chamada da suite chega sem chave — inclusive as que deveriam. A exigencia em si
    e verificada por `test_sem_chave_de_idempotencia_e_400`, que usa um cliente sem o gancho.
    """

    def por_requisicao(request: httpx.Request) -> None:
        if "Idempotency-Key" not in request.headers:
            request.headers["Idempotency-Key"] = str(uuid4())

    cliente = TestClient(app, headers={"Authorization": f"Bearer {token}"})
    # Pendurado depois da construcao: `TestClient.__init__` nao aceita `event_hooks`, mas ele
    # **herda** de `httpx.Client`, e o atributo existe no objeto pronto.
    cliente.event_hooks["request"].append(por_requisicao)
    return cliente


@pytest.fixture
def client(
    settings_teste: Settings, repositorio: RepositorioAnalisesMemoria, chaves_de_teste: Path
) -> Iterator[TestClient]:
    """Cliente HTTP com bureau limpo — isola o efeito do score."""
    app = criar_app(settings=settings_teste, repositorio=repositorio, bureau=BureauSempreLimpo())
    with montar_cliente(app, emitir_token(chaves_de_teste)) as c:
        yield c


@pytest.fixture
def client_restrito(
    settings_teste: Settings, repositorio: RepositorioAnalisesMemoria, chaves_de_teste: Path
) -> Iterator[TestClient]:
    """Cliente cujo bureau sempre acusa restricao — exercita o veto duro."""
    app = criar_app(settings=settings_teste, repositorio=repositorio, bureau=BureauSempreRestrito())
    with montar_cliente(app, emitir_token(chaves_de_teste)) as c:
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
