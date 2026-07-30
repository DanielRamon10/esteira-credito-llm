"""Fixtures compartilhadas.

A base de teste e definida aqui, e nao lida de `conhecimento/`: teste que depende de
arquivo de conteudo quebra quando alguem reescreve um artigo por outro motivo. O
carregamento do disco tem teste proprio, com arquivo temporario.

A excecao e o eval, que roda contra o corpus **real** de proposito — a medicao de
qualidade da busca sobre um corpus inventado nao diz nada sobre o corpus servido.
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

from customer_support.api import seguranca
from customer_support.config import Ambiente, ProvedorLLM, Settings
from customer_support.domain.conhecimento import Artigo
from customer_support.domain.divulgacao import Visibilidade

# --------------------------------------------------------- Autenticacao (C7)
#
# As chaves sao geradas **no import** deste arquivo, e nao numa fixture: autenticacao nao tem
# modo desligado, entao qualquer caminho que leia a configuracao do processo levanta sem elas —
# inclusive durante a coleta, antes de qualquer fixture existir.
#
# O par vive num diretorio temporario e **nunca** aparece literal aqui: o `.githooks/pre-commit`
# bloqueia PEM, e a saida certa e o teste nao ter o padrao — nao enfraquecer o scanner.
_DIRETORIO_DE_CHAVES = Path(tempfile.mkdtemp(prefix="sup-chaves-"))
emissor_local.gerar_chaves(_DIRETORIO_DE_CHAVES)
CHAVE_PUBLICA_DE_TESTE = emissor_local.chave_publica(_DIRETORIO_DE_CHAVES)

os.environ.setdefault("SUP_AUTH_CHAVE_PUBLICA", CHAVE_PUBLICA_DE_TESTE)
os.environ.setdefault("SUP_AUTH_EMISSOR", emissor_local.EMISSOR_LOCAL)


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
    audiencia: str = "customer-support",
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


class ConhecimentoFalso:
    """Base em memoria, com o mesmo contrato do adapter de arquivos."""

    def __init__(self, artigos: list[Artigo]) -> None:
        from plataforma.bm25 import IndiceBM25

        self._artigos = artigos
        self._publicos = [a for a in artigos if a.publico]
        self._indice_publico = IndiceBM25([a.texto_para_indexar for a in self._publicos])
        self._indice_completo = IndiceBM25([a.texto_para_indexar for a in artigos])

    def buscar(self, pergunta: str, k: int = 3, apenas_publicos: bool = True):  # type: ignore[no-untyped-def]
        from customer_support.domain.conhecimento import ArtigoRecuperado

        fonte = self._publicos if apenas_publicos else self._artigos
        indice = self._indice_publico if apenas_publicos else self._indice_completo
        if not pergunta.strip() or k <= 0:
            return []
        return [
            ArtigoRecuperado(artigo=fonte[i.indice], score=i.score)
            for i in indice.buscar(pergunta, k=k)
        ]

    def todos(self) -> list[Artigo]:
        return list(self._artigos)

    @property
    def total(self) -> int:
        return len(self._artigos)

    @property
    def publicos(self) -> int:
        return len(self._publicos)

    @property
    def procedencia(self) -> str:
        return "falso"


@pytest.fixture
def artigos() -> list[Artigo]:
    return [
        Artigo(
            id="comprovacao-renda",
            titulo="Como comprovar renda",
            texto=(
                "Assalariado envia os tres ultimos holerites. Autonomo envia o extrato "
                "bancario dos ultimos seis meses consecutivos."
            ),
        ),
        Artigo(
            id="portabilidade",
            titulo="Portabilidade de emprestimo",
            texto=(
                "Portabilidade e transferir seu emprestimo de outra instituicao para ca. "
                "Nao ha tarifa para o cliente."
            ),
        ),
        # Artigo INTERNO: existe para provar que a busca do cliente nao o alcanca.
        Artigo(
            id="limiares-internos",
            titulo="Limiares de score e alcadas",
            texto=(
                "Score acima de 700 pontos aprova direto. A alcada do gerente regional "
                "vai ate R$ 150.000. Conforme a POL-001, comprometimento acima de 50% "
                "e vedado."
            ),
            visibilidade=Visibilidade.INTERNA,
        ),
    ]


@pytest.fixture
def conhecimento(artigos: list[Artigo]) -> ConhecimentoFalso:
    return ConhecimentoFalso(artigos)


@pytest.fixture
def settings_teste() -> Settings:
    return Settings(
        ambiente=Ambiente.LOCAL,
        nivel_log="WARNING",
        log_json=False,
        # Explicito, e nao `auto`: com o Ollama instalado na maquina, o modo
        # automatico faria a suite chamar um modelo de verdade. Mesmo defeito que
        # apareceu no credit-analysis, ja resolvido aqui.
        provedor_llm=ProvedorLLM.ARTIGO,
        # Autenticacao nao tem modo desligado (ver api/seguranca.py): a suite precisa de
        # uma chave de verificacao real, e ela e a da sessao — chave literal no repositorio
        # e chave publicada.
        auth_chave_publica=CHAVE_PUBLICA_DE_TESTE,
        auth_emissor=emissor_local.EMISSOR_LOCAL,
    )
