"""Fixtures compartilhadas.

As entradas de teste sao definidas aqui e nao lidas de `dados/listas`: teste que
depende de arquivo de dado quebra quando alguem edita o arquivo por outro motivo.
O carregamento do CSV tem teste proprio, com arquivo temporario.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Sequence
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from plataforma import emissor_local

from kyc_compliance.api import seguranca
from kyc_compliance.config import Ambiente, Settings
from kyc_compliance.domain.triagem import EntradaRestritiva, TipoLista

# CPF valido que NAO pertence a ninguem nas listas de teste. Existe como constante
# porque reusar o CPF de uma entrada por descuido faz o teste passar pelo motivo
# errado — aconteceu de verdade durante a construcao deste servico.
CPF_LIMPO = "390.533.447-05"

CPF_DA_PEP = "529.982.247-25"
CPF_DO_SANCIONADO = "111.444.777-35"


# --------------------------------------------------------- Autenticacao (C7)
#
# As chaves sao geradas **no import** deste arquivo, e nao numa fixture: autenticacao nao tem
# modo desligado, entao qualquer caminho que leia a configuracao do processo levanta sem elas —
# inclusive durante a coleta, antes de qualquer fixture existir.
#
# O par vive num diretorio temporario e **nunca** aparece literal aqui: o `.githooks/pre-commit`
# bloqueia PEM, e a saida certa e o teste nao ter o padrao — nao enfraquecer o scanner.
_DIRETORIO_DE_CHAVES = Path(tempfile.mkdtemp(prefix="kyc-chaves-"))
emissor_local.gerar_chaves(_DIRETORIO_DE_CHAVES)
CHAVE_PUBLICA_DE_TESTE = emissor_local.chave_publica(_DIRETORIO_DE_CHAVES)

os.environ.setdefault("KYC_AUTH_CHAVE_PUBLICA", CHAVE_PUBLICA_DE_TESTE)
os.environ.setdefault("KYC_AUTH_EMISSOR", emissor_local.EMISSOR_LOCAL)


@pytest.fixture(scope="session")
def chaves_de_teste() -> Path:
    return _DIRETORIO_DE_CHAVES


@pytest.fixture(scope="session")
def chave_publica_de_teste() -> str:
    return CHAVE_PUBLICA_DE_TESTE


def emitir_token(
    chaves: Path = _DIRETORIO_DE_CHAVES,
    *,
    escopos: Sequence[str] = seguranca.TODOS_OS_ESCOPOS,
    audiencia: str = "kyc-compliance",
    # Parametros explicitos e nao `**kwargs`: com `**kwargs: object` o `--strict` recusa o
    # repasse para `emissor_local.emitir`, e a saida facil seria `Any` — perder tipagem num
    # helper que constroi credencial e onde menos se quer.
    emissor: str = emissor_local.EMISSOR_LOCAL,
    validade_segundos: int = emissor_local.VALIDADE_PADRAO_SEGUNDOS,
    agora: int | None = None,
    locatario: str | None = None,
) -> str:
    """Token de teste. O default carrega **todos** os escopos.

    Conveniencia deliberada, com custo assumido: os testes existentes nao provam autorizacao,
    porque passam com credencial total. Quem cobre autorizacao e
    `tests/integration/test_autenticacao.py`, com token restrito de proposito.

    A alternativa — escopo minimo por teste — faria cada mudanca de escopo quebrar dezenas de
    testes que nao tratam disso, e o reflexo seria afrouxar o escopo.
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


def montar_cliente(app: FastAPI, token: str | None = None) -> TestClient:
    """`TestClient` com `Authorization` em todas as requisicoes.

    No construtor e nao por chamada: com centenas de requisicoes na suite, passar por chamada
    garantiria que alguma ficasse sem — e falharia com 401 por esquecimento, o que treina a ler
    401 como ruido.
    """
    return TestClient(app, headers={"Authorization": f"Bearer {token or emitir_token()}"})


@pytest.fixture
def entradas() -> list[EntradaRestritiva]:
    return [
        EntradaRestritiva(
            nome="JOSE DA SILVA JR.",
            tipo=TipoLista.PEP,
            origem="teste-pep",
            cargo="Prefeito municipal",
        ),
        # Mesma pessoa em duas listas: e o caso que um indice por nome colapsaria.
        EntradaRestritiva(
            nome="JOSE DA SILVA JR.",
            tipo=TipoLista.SANCAO,
            origem="teste-sancoes",
        ),
        EntradaRestritiva(
            nome="MARIA FERNANDA SOUZA",
            tipo=TipoLista.PEP,
            origem="teste-pep",
            cpf=CPF_DA_PEP,
        ),
        EntradaRestritiva(
            nome="CARLOS E. LIMA",
            tipo=TipoLista.PEP,
            origem="teste-pep",
        ),
        EntradaRestritiva(
            nome="MARCOS VINICIUS TEIXEIRA",
            tipo=TipoLista.SANCAO,
            origem="teste-sancoes",
            cpf=CPF_DO_SANCIONADO,
        ),
        EntradaRestritiva(
            nome="JOSE ANTONIO PEREIRA",
            tipo=TipoLista.SANCAO,
            origem="teste-sancoes",
        ),
        EntradaRestritiva(
            nome="PATRICIA GOMES DE OLIVEIRA",
            tipo=TipoLista.MIDIA_NEGATIVA,
            origem="teste-midia",
        ),
    ]


@pytest.fixture
def settings_teste() -> Settings:
    return Settings(
        ambiente=Ambiente.LOCAL,
        nivel_log="WARNING",
        log_json=False,
        # Autenticacao nao tem modo desligado (ver api/seguranca.py): a suite precisa de
        # uma chave de verificacao real, e ela e a da sessao — chave literal no repositorio
        # e chave publicada.
        auth_chave_publica=CHAVE_PUBLICA_DE_TESTE,
        auth_emissor=emissor_local.EMISSOR_LOCAL,
    )
