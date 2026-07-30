"""Autenticacao na borda HTTP.

## Escopos deste servico

    analises:ler         GET  /v1/analises, GET /v1/analises/{id}
    analises:escrever    POST /v1/analises
    documentos:enviar    POST /v1/analises/{id}/documentos
    politicas:consultar  GET  /v1/politicas/buscar, POST /v1/politicas/consultar
    agente:consultar     POST /v1/agente/consultar

Cinco escopos e nao um `credito:tudo`, e a granularidade tem consequencia pratica: o canal
que **cria** proposta nao precisa poder ler proposta alheia, e o painel de analista que **le**
nao precisa poder enviar documento. Escopo unico transforma qualquer credencial vazada em
acesso total.

`documentos:enviar` e separado de `analises:escrever` porque enviar documento e o unico
caminho pelo qual conteudo nao confiavel entra neste servico — a superficie de OCR e de
prompt injection. Quem so registra proposta estruturada nao deveria abri-la.

## Nao existe modo desligado, e essa e a decisao mais importante do arquivo

Nao ha `CREDIT_AUTH_HABILITADO`. Autenticacao que se desliga por variavel de ambiente e
autenticacao que **vai** estar desligada: em algum ambiente, por algum motivo temporario que
ninguem reverteu, e sem nada falhando para avisar.

O mesmo raciocinio que fez `CREDIT_PROVEDOR_LLM` ser `ollama` explicito em producao em vez de
`auto`: um servico de credito respondendo com texto falso porque o LLM caiu e pior que um
servico fora do ar. Aqui e mais forte ainda — um servico de credito respondendo dado pessoal
a quem nao se identificou e pior que um servico fora do ar.

O custo e um comando a mais para rodar local:

    python -m plataforma.emissor_local gerar-chaves

Sem conta em provedor nenhum, o que preserva a restricao declarada do projeto.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Annotated, Any

import structlog
from fastapi import Depends, Header, Request
from plataforma import autenticacao as auth

from credit_analysis.api.deps import obter_settings
from credit_analysis.config import Settings

logger = structlog.get_logger(__name__)

# Escopos como constantes, e nao string literal na assinatura de cada rota.
#
# Um erro de digitacao em `dependencies=[Depends(Escopo("analises:lerr"))]` produziria um
# escopo que **nenhum token tem** — a rota devolveria 403 para todo mundo. Isso quebra alto,
# o que e bom. O caso ruim e o inverso: `Escopo("analises:ler")` onde se queria
# `analises:escrever` libera escrita para quem so tem leitura, e nada falha.
ANALISES_LER = "analises:ler"
ANALISES_ESCREVER = "analises:escrever"
DOCUMENTOS_ENVIAR = "documentos:enviar"
POLITICAS_CONSULTAR = "politicas:consultar"
AGENTE_CONSULTAR = "agente:consultar"

TODOS_OS_ESCOPOS = (
    ANALISES_LER,
    ANALISES_ESCREVER,
    DOCUMENTOS_ENVIAR,
    POLITICAS_CONSULTAR,
    AGENTE_CONSULTAR,
)


def _chaveiro(request: Request) -> auth.Chaveiro:
    chaveiro: auth.Chaveiro = request.app.state.chaveiro
    return chaveiro


async def identidade_do_pedido(
    request: Request,
    # `Depends(obter_settings)` e nao `Depends()`.
    #
    # `Annotated[Settings, Depends()]` parece funcionar e nao funciona: sem callable, o
    # FastAPI usa o **tipo** como dependencia e tenta construir `Settings` a partir do corpo
    # da requisicao. Como `BaseSettings` tem atributos privados (`_cli_parse_args`), o
    # Pydantic levanta `NameError: Fields must not use names with leading underscores` no
    # registro da rota — erro que nao menciona nem `Depends` nem autenticacao.
    settings: Annotated[Settings, Depends(obter_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> auth.Identidade:
    """Valida o token e guarda a identidade no `request.state`.

    Guardar no `state` importa para a trilha de auditoria: o middleware de log precisa do
    `sub` para registrar quem chamou, e ele roda **depois** da rota, quando a dependencia ja
    saiu de escopo.
    """
    identidade = auth.verificar(
        auth.extrair_do_cabecalho(authorization),
        chaveiro=_chaveiro(request),
        emissor=settings.auth_emissor,
        audiencia=settings.auth_audiencia,
    )
    request.state.identidade = identidade
    return identidade


IdentidadeDep = Annotated[auth.Identidade, Depends(identidade_do_pedido)]


def Escopo(*exigidos: str) -> Callable[..., Coroutine[Any, Any, auth.Identidade]]:  # noqa: N802
    """Fabrica de dependencia que exige escopos.

    Nome com maiuscula de proposito, contra a convencao: no ponto de uso ele aparece como
    `Depends(Escopo(ANALISES_LER))`, ao lado de `Depends`, `Header` e `Query`, que tambem sao
    fabricas com nome capitalizado. Uma funcao `escopo(...)` ali pareceria um valor.

    Todos os escopos passados sao **conjuncao**: a rota exige todos. Nao ha versao com
    disjuncao, e a ausencia e deliberada — "qualquer um destes serve" e o tipo de regra que
    cresce silenciosamente ate nao significar nada.
    """
    for escopo in exigidos:
        if escopo not in TODOS_OS_ESCOPOS:
            # Falha no **import** da aplicacao, nao na primeira requisicao. Um escopo com
            # erro de digitacao devolveria 403 para todo mundo em producao, e o sintoma
            # ("ninguem consegue acessar esta rota") nao aponta para a causa.
            raise ValueError(f"escopo desconhecido: {escopo}. Conhecidos: {TODOS_OS_ESCOPOS}")

    async def verificar_escopos(identidade: IdentidadeDep) -> auth.Identidade:
        for escopo in exigidos:
            identidade.exigir(escopo)
        return identidade

    return verificar_escopos


def montar_chaveiro(settings: Settings) -> auth.Chaveiro:
    """Constroi o chaveiro a partir da configuracao, ou falha ao subir.

    Falhar aqui e o comportamento desejado: um servico que sobe sem saber verificar token
    teria de escolher entre recusar tudo (indisponivel, mas seguro) ou aceitar tudo
    (disponivel, e aberto). A terceira opcao — nao subir — e a unica que nao esconde o
    problema, e e a que o Kubernetes trata bem, mantendo os pods antigos no ar.
    """
    if settings.auth_jwks_url:
        return auth.Chaveiro.de_jwks(settings.auth_jwks_url)
    return auth.Chaveiro.de_chave_publica(settings.auth_chave_publica)
