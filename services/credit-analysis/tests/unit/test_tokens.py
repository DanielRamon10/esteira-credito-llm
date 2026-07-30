"""Credencial de servico para chamar o KYC.

O teste mais importante deste arquivo e `test_chamadas_simultaneas_renovam_uma_vez`. Sem o
lock, N requisicoes concorrentes que encontram o cache vazio disparam N chamadas ao IdP — e
no boot o cache esta sempre vazio, entao o pico e proporcional a concorrencia, exatamente
quando o servico esta subindo. Alguns IdPs limitam taxa de `client_credentials`, e o resultado
seria 429 no pior momento.

O segundo e `test_renova_antes_de_expirar`: sem margem, o token e usado ate o ultimo segundo,
o relogio do outro servico esta dois segundos adiantado, e o KYC responde 401 — que o
disjuntor le como indisponibilidade e converte em revisao humana para toda analise.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from credit_analysis.infrastructure.tokens import (
    MARGEM_DE_RENOVACAO_SEGUNDOS,
    TokenDeClientCredentials,
    TokenEstatico,
)


class RelogioFalso:
    """Relogio monotonico controlavel.

    Injetado em vez de `time.monotonic` real para que os testes de expiracao nao durmam nem
    dependam do relogio da maquina — teste dependente de relogio ja apareceu neste projeto, na
    camada de observabilidade.
    """

    def __init__(self) -> None:
        self.agora = 1000.0

    def __call__(self) -> float:
        return self.agora

    def avancar(self, segundos: float) -> None:
        self.agora += segundos


class IdpFalso:
    """Endpoint de token que conta chamadas e devolve um token novo a cada vez."""

    def __init__(self, expires_in: int | None = 3600, status: int = 200) -> None:
        self.chamadas = 0
        self.corpos: list[dict[str, object]] = []
        self.autenticacoes: list[tuple[str, str] | None] = []
        self._expires_in = expires_in
        self._status = status

    async def __call__(self, pedido: httpx.Request) -> httpx.Response:
        # `sleep(0)` cede ao event loop, e sem isso o teste de concorrencia **nao concorre**.
        #
        # Descoberto por mutacao: removendo a segunda leitura do cache dentro do lock, o teste
        # continuava passando. A causa e que `MockTransport` com handler que nao faz I/O resolve
        # sem suspender — a primeira corrotina rodava inteira, populava o cache, e as outras 19
        # o encontravam cheio na primeira checagem, antes do lock. Nao havia intercalacao para o
        # lock evitar, e o teste media outra coisa.
        await asyncio.sleep(0)
        self.chamadas += 1
        self.corpos.append(dict(httpx.QueryParams(pedido.content.decode())))
        cabecalho = pedido.headers.get("Authorization")
        self.autenticacoes.append(cabecalho.split(" ", 1)[1] if cabecalho else None)  # type: ignore[union-attr]

        if self._status != 200:
            return httpx.Response(self._status, json={"error": "invalid_client"})

        corpo: dict[str, object] = {"access_token": f"token-{self.chamadas}"}
        if self._expires_in is not None:
            corpo["expires_in"] = self._expires_in
        return httpx.Response(200, json=corpo)


def montar(
    idp: IdpFalso, relogio: RelogioFalso | None = None
) -> tuple[TokenDeClientCredentials, RelogioFalso]:
    relogio = relogio or RelogioFalso()
    provedor = TokenDeClientCredentials(
        url_do_token="https://idp.invalid/oauth/token",
        client_id="credit-analysis",
        client_secret="segredo-de-teste",
        audiencia="kyc-compliance",
        cliente=httpx.AsyncClient(transport=httpx.MockTransport(idp)),
        agora=relogio,
    )
    return provedor, relogio


class TestTokenEstatico:
    async def test_devolve_o_configurado(self) -> None:
        assert await TokenEstatico("abc.def.ghi").obter() == "abc.def.ghi"

    async def test_apara_espaco(self) -> None:
        """Token colado de um `cat` traz `\\n`, e um `Authorization` com newline e invalido."""
        assert await TokenEstatico("  abc.def.ghi\n").obter() == "abc.def.ghi"

    def test_recusa_vazio(self) -> None:
        """Falhar na construcao, nao na primeira chamada ao KYC.

        Com string vazia, o cabecalho sairia como `Bearer ` e o KYC responderia 401 — que o
        disjuntor le como indisponibilidade, mandando toda analise para revisao humana por
        causa de uma variavel de ambiente vazia.
        """
        with pytest.raises(ValueError, match="vazio"):
            TokenEstatico("   ")


class TestClientCredentials:
    async def test_pede_o_grant_e_a_audiencia_certos(self) -> None:
        """`audience` explicita: sem ela, IdPs multi-audiencia emitem para a default.

        E um token com a audiencia errada e recusado pelo KYC com 401 — de novo lido como
        indisponibilidade.
        """
        idp = IdpFalso()
        provedor, _ = montar(idp)

        await provedor.obter()

        assert idp.corpos[0]["grant_type"] == "client_credentials"
        assert idp.corpos[0]["audience"] == "kyc-compliance"

    async def test_o_segredo_vai_no_cabecalho_e_nao_no_corpo(self) -> None:
        """`client_secret_basic`, como recomenda a RFC 6749 secao 2.3.1.

        Corpo de POST aparece em log de proxy e em captura de trafego com mais frequencia que
        cabecalho `Authorization`.
        """
        idp = IdpFalso()
        provedor, _ = montar(idp)

        await provedor.obter()

        assert "client_secret" not in idp.corpos[0]
        assert idp.autenticacoes[0] is not None

    async def test_cacheia_entre_chamadas(self) -> None:
        idp = IdpFalso()
        provedor, _ = montar(idp)

        primeiro = await provedor.obter()
        segundo = await provedor.obter()

        assert primeiro == segundo
        assert idp.chamadas == 1

    async def test_renova_antes_de_expirar(self) -> None:
        """A margem existe para nao usar o token ate o ultimo segundo.

        Os numeros sao **absolutos** e nao derivados de `MARGEM_DE_RENOVACAO_SEGUNDOS`. A
        primeira versao escrevia `avancar(3600 - MARGEM - 1)`, e por isso era insensivel ao
        valor da constante: zerando a margem, o teste continuava verde porque os proprios
        limites se moviam com ela. Foi um teste de mutacao que expos isso.

        Com `expires_in=3600`, a renovacao tem de acontecer **antes** de 3600s.
        """
        idp = IdpFalso(expires_in=3600)
        provedor, relogio = montar(idp)

        primeiro = await provedor.obter()

        # 3550s: dentro da validade nominal (3600) e depois do ponto de renovacao (3540).
        relogio.avancar(3550)
        segundo = await provedor.obter()

        assert segundo != primeiro, "o token nao foi renovado antes de expirar"
        assert idp.chamadas == 2

    async def test_a_margem_cobre_a_folga_de_relogio_da_plataforma(self) -> None:
        """A margem precisa ser maior que a tolerancia de relogio de quem valida.

        `plataforma.autenticacao.FOLGA_DE_RELOGIO_SEGUNDOS` e 30s: se a margem fosse menor, o
        token poderia sair daqui valido e chegar ao KYC ja recusado — 401 que o disjuntor le
        como indisponibilidade, mandando toda analise para revisao humana.

        Este teste e o que detecta um zero na constante; o de cima mede o mecanismo.
        """
        from plataforma.autenticacao import FOLGA_DE_RELOGIO_SEGUNDOS

        assert MARGEM_DE_RENOVACAO_SEGUNDOS > FOLGA_DE_RELOGIO_SEGUNDOS

    def test_o_relogio_padrao_e_monotonico(self) -> None:
        """`time.monotonic`, nao `time.time`.

        Unica mutacao que a suite nao pegava por comportamento, e a razao e legitima: os testes
        injetam relogio falso, entao o default nunca e exercitado. Uma assercao direta e o guard
        correto — inventar um teste que manipulasse o relogio do sistema seria pior que isto.

        Importa porque `time.time` anda para tras: um ajuste de NTP faria o cache expirar cedo
        (chamada extra ao IdP, inofensivo) ou **tarde** (token expirado em uso, 401 vindo do KYC
        e lido como indisponibilidade pelo disjuntor).
        """
        import inspect
        import time

        padrao = inspect.signature(TokenDeClientCredentials.__init__).parameters["agora"].default

        assert padrao is time.monotonic

    async def test_usa_o_token_cacheado_bem_antes_do_vencimento(self) -> None:
        """A contrapartida: a margem nao pode ser tao grande que renove a cada chamada."""
        idp = IdpFalso(expires_in=3600)
        provedor, relogio = montar(idp)

        primeiro = await provedor.obter()
        relogio.avancar(1800)  # metade da validade

        assert await provedor.obter() == primeiro
        assert idp.chamadas == 1

    async def test_chamadas_simultaneas_renovam_uma_vez(self) -> None:
        """O lock, e a segunda leitura dentro dele.

        Sem o lock, 20 chamadas concorrentes com cache vazio produzem 20 requisicoes ao IdP.
        Com lock mas **sem** reconferir o cache apos adquiri-lo, elas serializam e ainda
        produzem 20 — o lock evitaria a concorrencia, nao as chamadas.
        """
        idp = IdpFalso()
        provedor, _ = montar(idp)

        tokens = await asyncio.gather(*(provedor.obter() for _ in range(20)))

        assert len(set(tokens)) == 1
        assert idp.chamadas == 1

    async def test_sem_expires_in_usa_um_default_curto(self) -> None:
        """`expires_in` e opcional na RFC 6749.

        Sem default, um `KeyError` aqui viraria falha de triagem. Com default longo demais, um
        token revogado continuaria em uso.
        """
        idp = IdpFalso(expires_in=None)
        provedor, relogio = montar(idp)

        await provedor.obter()
        relogio.avancar(300)
        await provedor.obter()

        assert idp.chamadas == 2

    async def test_erro_do_idp_propaga(self) -> None:
        """Nao ha cache de falha nem fallback.

        Um fallback para token estatico aqui transformaria indisponibilidade do IdP em uso de
        credencial possivelmente expirada — o cliente do KYC classifica esta excecao como
        transitoria, e o disjuntor cuida do resto.
        """
        provedor, _ = montar(IdpFalso(status=401))

        with pytest.raises(httpx.HTTPStatusError):
            await provedor.obter()

    async def test_falha_nao_envenena_o_cache(self) -> None:
        """Depois de uma falha, a proxima chamada tenta de novo.

        Se a excecao deixasse `_expira_em` avancado, o provedor devolveria `None` ou um token
        velho na tentativa seguinte.
        """
        idp = IdpFalso(status=500)
        provedor, _ = montar(idp)

        for _ in range(2):
            with pytest.raises(httpx.HTTPStatusError):
                await provedor.obter()

        assert idp.chamadas == 2

    async def test_o_token_nao_aparece_no_log(self, caplog: pytest.LogCaptureFixture) -> None:
        """Log e de onde credencial vaza para print de Slack.

        Nem truncado: o header de um JWT revela o algoritmo, e o corpo revela emissor e
        audiencia em base64 trivialmente decodificavel.
        """
        idp = IdpFalso()
        provedor, _ = montar(idp)

        with caplog.at_level("INFO"):
            token = await provedor.obter()

        assert token not in caplog.text
