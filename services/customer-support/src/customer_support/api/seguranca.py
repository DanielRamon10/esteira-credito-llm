"""Autenticacao na borda HTTP.

## Um escopo, e a razao de nao serem dois

    atendimentos:criar  POST /v1/atendimentos

O consumidor desta API e **um**: o canal de atendimento. Nao ha painel de leitura porque nao
ha endpoint de leitura — este servico nao guarda historico de atendimento, e essa ausencia e
anterior a esta camada.

Inventar `atendimentos:ler` aqui seria pior que nao ter: um escopo que nenhuma rota exige
aparece na documentacao como capacidade existente, e alguem eventualmente o concede a um
cliente que passaria a acreditar que pode ler algo.

## Por que autenticar um servico voltado ao publico, se o cliente final nao tem credencial

O cliente final nao chama esta API. Quem chama e o canal — app, site, chatbot, atendente —
e e o canal que se autentica. A distincao importa para o que **nao** esta aqui: nao ha login
de usuario final, nao ha refresh token, nao ha sessao. Este servico nunca sabe quem e a
pessoa do outro lado, e nao deveria: ele responde duvida sobre produto a partir de artigo
publico, e a decisao de encaminhar reclamacao a ouvidoria e deterministica.

Isso tambem e o que torna a **fronteira de divulgacao** independente de autenticacao. Um
canal legitimo, com token valido, continua nao conseguindo extrair limiar interno de score:
o filtro de visibilidade na entrada, o guard na saida e a ausencia de rota de rede para os
servicos internos nao dependem de quem pergunta. Autenticacao aqui responde "quem esta
usando o canal?", nao "isto pode ser revelado?".
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import Depends, Header, Request
from plataforma import autenticacao as auth

from customer_support.api.deps import SettingsDep

ATENDIMENTOS_CRIAR = "atendimentos:criar"

TODOS_OS_ESCOPOS = (ATENDIMENTOS_CRIAR,)


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
    for escopo in exigidos:
        if escopo not in TODOS_OS_ESCOPOS:
            raise ValueError(f"escopo desconhecido: {escopo}. Conhecidos: {TODOS_OS_ESCOPOS}")

    async def verificar_escopos(identidade: IdentidadeDep) -> auth.Identidade:
        for escopo in exigidos:
            identidade.exigir(escopo)
        return identidade

    return verificar_escopos


def montar_chaveiro(settings: Any) -> auth.Chaveiro:
    if settings.auth_jwks_url:
        return auth.Chaveiro.de_jwks(settings.auth_jwks_url)
    return auth.Chaveiro.de_chave_publica(settings.auth_chave_publica)
