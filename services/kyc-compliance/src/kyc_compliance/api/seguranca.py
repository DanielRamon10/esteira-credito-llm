"""Autenticacao na borda HTTP.

## Escopos deste servico

    triagens:executar  POST /v1/triagens
    triagens:ler       GET  /v1/triagens, GET /v1/triagens/{id}

Dois, e a separacao nao e simetria com o `credit-analysis`: ela existe porque os dois
consumidores sao diferentes. Quem **executa** triagem e a esteira de credito, de forma
sincrona, no meio de uma analise. Quem **le** triagem passada e o time de conformidade,
respondendo auditoria.

Dar `triagens:ler` a esteira daria a ela a capacidade de varrer o historico de quem foi
triado — que e a lista de pessoas em situacao sensivel, e nao um dado que a esteira precisa.

## A terceira defesa, e por que ela nao substitui esta

Este servico tem NetworkPolicy que aceita ingress **somente** do pod do `credit-analysis`
(verificado num cluster real: um pod rotulado `customer-support` nao alcanca a porta). A
autenticacao aqui e a segunda camada, e as duas cobrem casos diferentes:

- a rede impede que outro **pod** alcance o servico, e nao impede que o pod autorizado
  chame o que nao deveria — um bug no `credit-analysis` que consultasse o historico inteiro
  passaria pela policy;
- o token impede que **qualquer chamador** faca o que o escopo dele nao permite, e nao
  impede que um pod comprometido com credencial valida faca o que a credencial permite.

Nenhuma das duas e redundante. E vale dizer o que **nao** e defesa: o servico nao estar
exposto na internet. Um servico interno alcancavel de dentro da VPC e alcancavel por
qualquer coisa comprometida ali.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import Depends, Header, Request
from plataforma import autenticacao as auth

from kyc_compliance.api.deps import SettingsDep

TRIAGENS_EXECUTAR = "triagens:executar"
TRIAGENS_LER = "triagens:ler"

TODOS_OS_ESCOPOS = (TRIAGENS_EXECUTAR, TRIAGENS_LER)


async def identidade_do_pedido(
    request: Request,
    settings: SettingsDep,
    authorization: Annotated[str | None, Header()] = None,
) -> auth.Identidade:
    identidade = auth.verificar(
        auth.extrair_do_cabecalho(authorization),
        chaveiro=request.app.state.chaveiro,
        emissor=settings.auth_emissor,
        audiencia=settings.auth_audiencia,
    )
    request.state.identidade = identidade
    return identidade


IdentidadeDep = Annotated[auth.Identidade, Depends(identidade_do_pedido)]


def Escopo(*exigidos: str) -> Callable[..., Coroutine[Any, Any, auth.Identidade]]:  # noqa: N802
    """Fabrica de dependencia que exige escopos, todos em conjuncao."""
    for escopo in exigidos:
        if escopo not in TODOS_OS_ESCOPOS:
            # Falha na construcao da aplicacao, nao na primeira requisicao: um escopo com
            # erro de digitacao devolveria 403 para todo mundo, e o sintoma nao aponta para a
            # causa.
            raise ValueError(f"escopo desconhecido: {escopo}. Conhecidos: {TODOS_OS_ESCOPOS}")

    async def verificar_escopos(identidade: IdentidadeDep) -> auth.Identidade:
        for escopo in exigidos:
            identidade.exigir(escopo)
        return identidade

    return verificar_escopos


def montar_chaveiro(settings: Any) -> auth.Chaveiro:
    """Constroi o chaveiro, ou falha ao subir.

    Falhar na subida e o comportamento desejado: um servico que nao sabe verificar token
    teria de escolher entre recusar tudo (indisponivel) ou aceitar tudo (aberto). Nao subir e
    a unica opcao que nao esconde o problema, e a que o Kubernetes trata bem — os pods
    antigos ficam no ar.
    """
    if settings.auth_jwks_url:
        return auth.Chaveiro.de_jwks(settings.auth_jwks_url)
    return auth.Chaveiro.de_chave_publica(settings.auth_chave_publica)
