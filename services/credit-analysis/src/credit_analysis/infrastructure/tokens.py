"""Obtencao de token para chamar outro servico.

## O problema que este modulo resolve

A partir da Camada 7 o `kyc-compliance` exige token com `aud=kyc-compliance`. O
`credit-analysis` recebe token com `aud=credit-analysis` e **nao pode repassa-lo**: seria
justamente a escalada lateral que a validacao de audiencia existe para impedir.

Ou seja, este servico precisa da propria credencial de servico — e o fluxo do OAuth 2.0 para
isso e `client_credentials` (RFC 6749 secao 4.4): nao ha usuario envolvido, o cliente se
autentica com o proprio segredo e recebe token para a audiencia que pedir.

## Duas implementacoes, e a segunda nao e um atalho

`TokenDeClientCredentials` e o caminho de producao: fala com o endpoint de token do IdP e
cacheia. `TokenEstatico` le um token pronto da configuracao, para desenvolvimento — o projeto
roda sem conta em provedor nenhum, e nao ha IdP local com endpoint de token.

A escolha e explicita na configuracao, nunca automatica. Um `auto` que caisse no estatico
quando o endpoint nao responde transformaria indisponibilidade do IdP em uso de credencial
possivelmente expirada, e o sintoma seria 401 intermitente vindo do KYC.
"""

from __future__ import annotations

import asyncio
import time
from typing import Protocol

import httpx
import structlog

logger = structlog.get_logger(__name__)

# Renova quando falta menos que isto para expirar, em vez de esperar o vencimento.
#
# Sem a margem, o token e usado ate o ultimo segundo e a corrida e garantida: a requisicao sai
# valida, o relogio do outro servico esta 2s adiantado, e o KYC responde 401. O disjuntor do
# `credit-analysis` leria isso como indisponibilidade — 5 falhas e toda analise vai para
# revisao humana, por causa de dois segundos.
#
# 60s cobre a folga de relogio da plataforma (30s) com sobra.
MARGEM_DE_RENOVACAO_SEGUNDOS = 60

# Timeout da chamada ao IdP. Curto de proposito: ela esta no caminho de uma analise de credito,
# e um IdP lento nao pode transformar cada analise em espera. Se estourar, a triagem falha como
# transitoria e o disjuntor cuida do resto.
TIMEOUT_PADRAO = 3.0


class ProvedorDeToken(Protocol):
    """Port. `infrastructure` depende disto, nao de `httpx`."""

    async def obter(self) -> str: ...


class TokenEstatico:
    """Token vindo da configuracao. Desenvolvimento e ambientes sem IdP.

    Nao valida nem inspeciona o token: se ele estiver expirado, o KYC devolve 401 e o
    disjuntor trata como indisponibilidade. Inspecionar aqui daria a impressao de que o
    problema esta resolvido — o token continuaria expirado, apenas com erro diferente.
    """

    __slots__ = ("_token",)

    def __init__(self, token: str) -> None:
        if not token.strip():
            raise ValueError("token estatico vazio")
        self._token = token.strip()

    async def obter(self) -> str:
        return self._token


class TokenDeClientCredentials:
    """`client_credentials` contra o endpoint de token do IdP, com cache.

    ## Por que o cache tem lock

    Sem ele, N requisicoes simultaneas que encontram o cache vazio disparam N chamadas ao IdP —
    e no boot, quando o cache esta sempre vazio, isso e um pico contra o IdP proporcional a
    concorrencia. Pior: alguns IdPs limitam taxa de `client_credentials`, e o resultado seria
    429 no momento em que o servico esta subindo.

    O lock e por instancia e nao global porque a instancia e por audiencia: um lock global
    faria a renovacao do token do KYC bloquear a de outro servico futuro.
    """

    __slots__ = (
        "_agora",
        "_audiencia",
        "_cliente",
        "_expira_em",
        "_id",
        "_lock",
        "_segredo",
        "_token",
        "_url",
    )

    def __init__(
        self,
        *,
        url_do_token: str,
        client_id: str,
        client_secret: str,
        audiencia: str,
        cliente: httpx.AsyncClient | None = None,
        agora: object = time.monotonic,
    ) -> None:
        self._url = url_do_token
        self._id = client_id
        self._segredo = client_secret
        self._audiencia = audiencia
        self._cliente = cliente or httpx.AsyncClient(timeout=TIMEOUT_PADRAO)
        self._token: str | None = None
        self._expira_em = 0.0
        self._lock = asyncio.Lock()
        # `time.monotonic` e nao `time.time`: ajuste de relogio (NTP, horario de verao) faria o
        # cache expirar cedo ou, pior, tarde. Monotonico nao anda para tras.
        self._agora = agora

    async def obter(self) -> str:
        agora = self._instante()
        if self._token is not None and agora < self._expira_em:
            return self._token

        async with self._lock:
            # Confere de novo dentro do lock: entre a checagem acima e a aquisicao, outra
            # corrotina pode ter renovado. Sem esta segunda leitura, o lock serializa as
            # chamadas mas nao evita nenhuma.
            agora = self._instante()
            if self._token is not None and agora < self._expira_em:
                return self._token
            return await self._renovar(agora)

    async def _renovar(self, agora: float) -> str:
        resposta = await self._cliente.post(
            self._url,
            data={
                "grant_type": "client_credentials",
                # `audience` explicita: sem ela, IdPs que suportam multiplas audiencias emitem
                # para a default — e um token com a audiencia errada e recusado pelo KYC com
                # 401, que o disjuntor le como indisponibilidade.
                "audience": self._audiencia,
            },
            # `client_secret_basic`, nao o segredo no corpo. Corpo de POST aparece em log de
            # proxy e em captura de trafego com mais frequencia que cabecalho `Authorization`,
            # e a RFC 6749 secao 2.3.1 recomenda o cabecalho.
            auth=(self._id, self._segredo),
        )
        resposta.raise_for_status()
        corpo = resposta.json()

        token = str(corpo["access_token"])
        # `expires_in` e opcional na RFC. Sem ele, 300s: curto o suficiente para que um token
        # revogado saia de circulacao rapido, e longo o suficiente para nao virar uma chamada
        # ao IdP por requisicao.
        validade = float(corpo.get("expires_in", 300))

        self._token = token
        self._expira_em = agora + max(validade - MARGEM_DE_RENOVACAO_SEGUNDOS, 0.0)

        logger.info(
            "token.renovado",
            audiencia=self._audiencia,
            validade_segundos=validade,
            # **Nunca** o token, nem truncado: o header de um JWT revela o algoritmo e o corpo
            # revela emissor e audiencia em base64 trivial. Log e de onde credencial vaza para
            # print de Slack.
        )
        return token

    def _instante(self) -> float:
        return float(self._agora())  # type: ignore[operator]

    async def fechar(self) -> None:
        await self._cliente.aclose()
