"""Carga sobre a API, contra Postgres real.

Como o eval de OCR, isto e **medicao versionada** e nao teste unitario. Rodar com:

    pytest -m carga

Exige Postgres. Nao roda no CI normal: a medicao depende de CPU, disco e do que mais roda na
maquina, e um piso numerico aqui mediria o runner.

## O que esta camada existe para responder

Tres alegacoes escritas no repositorio, argumentadas e nunca medidas:

1. **"o motor de score e puro CPU e responde em milissegundos"** — o docstring de `criar_analise`,
   desde a Camada 1. Nunca medido sob concorrencia;
2. **"escala independente"** — o cabecalho de `infra/k8s/base/credit-analysis/trabalhador.yaml`, que
   justifica o Deployment separado dizendo que dimensionar um nao deveria dimensionar o outro. A
   Camada 9 provou que o trabalhador **funciona** separado; nao mediu se separar entrega isso;
3. **"o dicionario cresce sem limite ... para o volume deste projeto e irrelevante"** — o comentario
   de `_VERSOES` em `postgres.py`. "Irrelevante" era uma estimativa.

## Medicao de referencia (2026-08, Windows 11, Postgres em container)

**Todos os numeros abaixo sao teto e nao previsao.** O Postgres do compose roda com `fsync=off`,
`synchronous_commit=off` e `full_page_writes=off` — deliberado e documentado no compose, porque
perder aquele banco custa um `down -v`. Com durabilidade ligada, escrita e mais lenta.

    camada                                    vazao          latencia
    scoring.avaliar (CPU puro, sem I/O)       15.674/s       p50 0,064ms
    caso de uso, repositorio em memoria        1.765/s (c=1)  p50 0,46ms
                                               2.010/s (c=10) p50 2,72ms
                                               1.690/s (c=50) p50 15,3ms
    caso de uso, repositorio Postgres              81/s (c=1)  p50 11,9ms
                                                  166/s (c=10) p50 49,8ms
                                                  174/s (c=50) p50 265ms
    POST /analises via ASGI                        41/s (c=1)  p50 24ms
                                                   55/s (c=10) p50 154ms
                                                   57/s (c=50) p50 810ms
    POST /analises via uvicorn no container         14/s (c=1)  p50 68ms
                                                   54/s (c=10) p50 177ms
                                                   46/s (c=50) p50 929ms
    GET /analises/{id} via ASGI                   101/s (c=50) p50 453ms

    pool, uma ida (SELECT 1)                      360/s serial  p50 2,78ms
    pool, SELECT 1 concorrente                  1.409/s (c=50)
    pool, INSERT concorrente                    1.183/s (c=8)
    pool, transacao de 4 comandos                             p50 5,95ms

## O que a medicao respondeu

**1. "Puro CPU e responde em milissegundos" (Camada 1) e conservador.** Sao 0,064ms — microssegundos
—, e o teto teorico do motor de score e ~15.700/s. Nao ha nada a otimizar ali.

**2. O teto da API e numero de idas ao banco, e nao CPU.** O mesmo caso de uso faz 1.765/s com
repositorio em memoria e 174/s com Postgres: **10x**, com o dominio identico. Somando autenticacao e
o contrato de idempotencia (que acrescenta reivindicar mais concluir), a rota fica em ~50/s.

Isso reforca com numero a anotacao `limitacao-conhecida` do HPA em `resiliencia.yaml`, que diz que
escalar por CPU e uma aproximacao: a API nao e limitada por CPU de forma nenhuma.

**3. Concorrencia acima de ~10 so acrescenta fila.** A vazao fica travada e a latencia cresce
linear:
50 requisicoes em voo dividido por 265ms da 189/s, que e o teto medido. Little's law explica a
tabela inteira, e a consequencia operacional e que aumentar concorrencia do cliente nao aumenta
vazao — aumenta o p95 dele.

**4. `_VERSOES` cresce uma entrada por analise, ~47 B/entrada no dicionario.** Projetando: 1 milhao
de analises vistas por um processo dao ~47MB no dicionario mais os objetos `UUID` e `int`
referenciados, na casa de 150MB. O comentario em `postgres.py` chamava isso de "irrelevante para o
volume deste projeto"; agora e um numero, e a estimativa se confirma para este volume.

**5. Compartilhar o Postgres custa ~18% de latencia sob a contencao testada** (p50 156ms -> 184ms).
E a metade da alegacao "escala independente" que o manifesto do trabalhador nao discute: separar
processos nao separa o banco.

## Uma hipotese que a medicao refutou

A primeira leitura foi que o teto vinha de trabalho de CPU dentro do event loop — score sincrono num
handler async serializaria tudo. Medido: o score custa 0,064ms, e o teto teorico dele e 300x o
observado. A hipotese estava errada.

A segunda foi fsync do WAL. Tambem errada, e por um motivo que valia conferir antes de escrever: o
compose **ja** roda com `fsync=off`. Escrita faz 1.183/s ali.

O que sobrou foi o mais simples: cada `POST` custa ~12 idas ao banco, e 12 x ~0,85ms sob
concorrencia da exatamente a ordem de grandeza medida. Sem defeito, sem misterio — e um numero que
diz onde mexer se algum dia precisar.

## Por que os pisos sao propriedades e nao numeros

A licao esta no eval de OCR: piso numerico sobre medicao dependente de ambiente passa a medir o
ambiente, e a reacao a um vermelho legitimo e afrouxar o numero. As assercoes aqui sao coisas que
nao mudam com a velocidade da maquina — nenhuma requisicao falha, nenhuma analise duplicada,
conflito de versao e sempre transitorio.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
import pytest
from psycopg_pool import AsyncConnectionPool

from credit_analysis.config import get_settings
from credit_analysis.infrastructure.repositories.postgres import _VERSOES
from tests.carga.driver import Amostra, medir, rodar, tabela

SUFIXO_TESTE = "_test"


def _dsn_de_teste() -> str:
    bruto = os.getenv("CREDIT_POSTGRES_DSN_TEST") or get_settings().postgres_dsn.strip()
    if not bruto:
        return ""
    partes = urlsplit(bruto)
    banco = partes.path.lstrip("/")
    if not banco.endswith(SUFIXO_TESTE):
        banco += SUFIXO_TESTE
    return urlunsplit(partes._replace(path=f"/{banco}"))


DSN = _dsn_de_teste()

pytestmark = [
    pytest.mark.carga,
    pytest.mark.skipif(not DSN, reason="CREDIT_POSTGRES_DSN nao definido"),
]

# Volumes pequenos de proposito.
#
# O objetivo nao e saturar a maquina — e responder as tres perguntas do cabecalho. 200 requisicoes
# com concorrencia 50 ja exercitam o pool de 8 conexoes com folga de 6x, que e onde o
# enfileiramento aparece. Numeros grandes deixariam a suite lenta sem medir nada novo.
REQUISICOES = 200
CONCORRENCIA_ALTA = 50


def payload(valor: str = "45000.00") -> dict[str, Any]:
    return {
        "solicitante": {
            "nome": "Ana Souza",
            "cpf": "529.982.247-25",
            "data_nascimento": "1990-05-14T00:00:00Z",
            "renda_mensal_declarada": "8500.00",
        },
        "proposta": {
            "valor_solicitado": valor,
            "prazo_meses": 36,
            "taxa_juros_mensal": "1.99",
        },
    }


@pytest.fixture
async def pool() -> AsyncIterator[AsyncConnectionPool]:
    p = AsyncConnectionPool(DSN, min_size=2, max_size=8, open=False)
    await p.open(wait=True, timeout=15)
    async with p.connection() as conexao:
        await conexao.execute("DELETE FROM analise")
        await conexao.execute("DELETE FROM idempotencia")
    _VERSOES.clear()
    yield p
    await p.close()


@pytest.fixture
async def cliente(
    pool: AsyncConnectionPool, chaves_de_teste: Any
) -> AsyncIterator[httpx.AsyncClient]:
    """Cliente ASGI direto contra o app, sem rede.

    ## Por que ASGI e nao HTTP no compose

    Duas razoes, e a segunda e a que decide:

    - **repetibilidade** — a stack do compose tem Ollama, MinIO e ElasticMQ no caminho, e a medicao
      passaria a incluir o que cada um estiver fazendo;
    - **atribuicao** — o que se quer medir e a aplicacao e o banco. Com rede e proxy no meio, um p95
      alto nao diz qual dos tres.

    O custo e nao medir o servidor HTTP de verdade: uvicorn, keep-alive e o custo de socket ficam de
    fora. Para as tres perguntas do cabecalho isso nao muda a resposta, e o smoke test do CI ja
    exercita a imagem servindo trafego.
    """
    from plataforma import emissor_local

    from credit_analysis.api.app import criar_app
    from credit_analysis.config import Ambiente, ProvedorLLM, Settings
    from credit_analysis.infrastructure.bureau import BureauSempreLimpo
    from credit_analysis.infrastructure.llm.anthropic_adapter import LLMFake
    from credit_analysis.infrastructure.repositories.postgres import RepositorioAnalisesPostgres

    settings = Settings(
        ambiente=Ambiente.LOCAL,
        nivel_log="WARNING",
        log_json=False,
        postgres_dsn="",
        anthropic_api_key="",
        provedor_llm=ProvedorLLM.FAKE,
        auth_chave_publica=emissor_local.chave_publica(chaves_de_teste),
        auth_emissor=emissor_local.EMISSOR_LOCAL,
        _env_file=None,  # type: ignore[call-arg]
    )
    app = criar_app(
        settings=settings,
        repositorio=RepositorioAnalisesPostgres(pool),
        bureau=BureauSempreLimpo(),
        llm=LLMFake(),
    )
    token = emissor_local.emitir(
        audiencia="credit-analysis",
        escopos=["analises:escrever", "analises:ler"],
        diretorio=chaves_de_teste,
    )

    transporte = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transporte,
        base_url="http://carga",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    ) as c:
        yield c


class TestCargaDeCriacao:
    @pytest.mark.parametrize("concorrencia", [1, 10, CONCORRENCIA_ALTA])
    async def test_criacao_sob_concorrencia(
        self, cliente: httpx.AsyncClient, concorrencia: int
    ) -> None:
        """Vazao e percentis de `POST /v1/analises`, e a propriedade que nao depende de velocidade.

        A assercao e **nenhuma falha**. Com pool de 8 e concorrencia 50, o caminho natural do
        defeito e esgotamento: requisicao esperando conexao, `PoolTimeout`, 500. Se acontecer, o
        `falhas` do resumo mostra o codigo — e um p95 baixo com metade das requisicoes em 500
        pareceria otimo sem o par.
        """

        async def criar(_: int) -> Amostra:
            return await medir(
                lambda: cliente.post(
                    "/v1/analises",
                    json=payload(),
                    headers={"Idempotency-Key": str(uuid4())},
                )
            )

        resultado = await rodar(criar, concorrencia=concorrencia, total=REQUISICOES)

        print(f"\n  POST /analises c={concorrencia} n={REQUISICOES}: {resultado.resumo()}")
        assert resultado.falhas == [], f"requisicoes falharam: {resultado.falhas}"
        assert resultado.sucessos == REQUISICOES

    async def test_leitura_sob_concorrencia(self, cliente: httpx.AsyncClient) -> None:
        """`GET` e o caminho mais quente de qualquer API, e o que mais sofre com pool pequeno."""
        criada = await cliente.post(
            "/v1/analises", json=payload(), headers={"Idempotency-Key": str(uuid4())}
        )
        assert criada.status_code == 201, criada.text
        analise_id = criada.json()["id"]

        async def ler(_: int) -> Amostra:
            return await medir(lambda: cliente.get(f"/v1/analises/{analise_id}"))

        resultado = await rodar(ler, concorrencia=CONCORRENCIA_ALTA, total=500)

        print(f"\n  GET /analises/id c={CONCORRENCIA_ALTA} n=500: {resultado.resumo()}")
        assert resultado.falhas == []


class TestIdempotenciaSobCarga:
    async def test_mesma_chave_em_rajada_cria_uma_analise(
        self, cliente: httpx.AsyncClient, pool: AsyncConnectionPool
    ) -> None:
        """A garantia da Camada 11 sob concorrencia real, e nao com duas chamadas.

        O teste de la dispara 8 reivindicacoes no repositorio; este manda 50 requisicoes HTTP
        simultaneas com a **mesma** chave e conta linhas na tabela `analise`. E a diferenca importa:
        entre a reivindicacao e o `concluir` existe o processamento inteiro — bureau, score,
        persistencia — e uma janela ali nao apareceria no teste de repositorio.
        """
        chave = str(uuid4())

        async def criar(_: int) -> Amostra:
            return await medir(
                lambda: cliente.post(
                    "/v1/analises", json=payload(), headers={"Idempotency-Key": chave}
                )
            )

        resultado = await rodar(criar, concorrencia=CONCORRENCIA_ALTA, total=CONCORRENCIA_ALTA)

        async with pool.connection() as conexao:
            cursor = await conexao.execute("SELECT count(*) FROM analise")
            linha = await cursor.fetchone()

        print(f"\n  rajada mesma chave n={CONCORRENCIA_ALTA}: {resultado.resumo()}")
        assert linha is not None
        assert linha[0] == 1, f"{linha[0]} analises criadas com a mesma chave"

        # Uma criou (201) e as outras receberam 200 (repeticao) ou 409 (em andamento). Nenhuma pode
        # ter falhado com 5xx: sob rajada, o desfecho e um dos tres, e nao erro.
        codigos = {a.status for a in resultado.amostras}
        assert codigos <= {201, 200, 409}, f"codigos inesperados: {sorted(codigos)}"
        assert sum(1 for a in resultado.amostras if a.status == 201) == 1


class TestCrescimentoDoCacheDeVersao:
    async def test_quantifica_o_custo_do_dicionario_de_versoes(
        self, cliente: httpx.AsyncClient
    ) -> None:
        """`_VERSOES` cresce sem limite, e o comentario dele chama isso de irrelevante.

        Este teste troca a estimativa por um numero. Ele nao afirma um teto — afirma que o
        crescimento e **linear e conhecido**, e imprime bytes por analise para o comentario poder
        citar medida em vez de adjetivo.

        Nao ha assercao de memoria maxima: ela dependeria da versao do CPython e do tamanho de
        ponteiro, e viraria o tipo de piso que esta suite evita.
        """
        antes = len(_VERSOES)

        async def criar(_: int) -> Amostra:
            return await medir(
                lambda: cliente.post(
                    "/v1/analises", json=payload(), headers={"Idempotency-Key": str(uuid4())}
                )
            )

        await rodar(criar, concorrencia=10, total=REQUISICOES)

        crescimento = len(_VERSOES) - antes
        bytes_por_entrada = (sys.getsizeof(_VERSOES) / len(_VERSOES)) if _VERSOES else 0

        print(
            f"\n  _VERSOES: {antes} -> {len(_VERSOES)} entradas "
            f"(+{crescimento} para {REQUISICOES} analises), "
            f"~{bytes_por_entrada:.0f} B/entrada no dicionario"
        )

        # Uma entrada por analise criada: e o que "linear" significa, e o que torna a projecao
        # possivel. Se aparecer mais de uma por analise, ha vazamento por outra razao.
        assert crescimento == REQUISICOES


class TestEscalaIndependente:
    async def test_carga_da_api_com_o_banco_sob_contencao(
        self, cliente: httpx.AsyncClient, pool: AsyncConnectionPool
    ) -> None:
        """A alegacao do manifesto do trabalhador, medida do jeito que da para medir aqui.

        ## O que este teste mede, e o que ele nao mede

        "Escala independente" tem duas metades. A primeira — o trabalhador nao compete por CPU e
        memoria com a API — e verdade por construcao desde a Camada 9: sao processos separados, e
        isso nao precisa de medicao.

        A segunda e a que o manifesto nao discute: os dois **compartilham o Postgres**. Separar os
        processos nao separa o banco, e sob contencao a latencia da API pode subir por causa de
        trabalho do trabalhador.

        Aqui a contencao e produzida por consultas concorrentes no mesmo pool, e nao pelo
        trabalhador de verdade: subir Tesseract e MinIO dentro deste teste tornaria a medicao
        dependente de mais tres componentes e nao mudaria a conclusao — o gargalo compartilhado e o
        banco.

        A comparacao imprime as duas latencias. Nao ha assercao sobre a diferenca: ela e exatamente
        o tipo de numero que varia com a maquina.
        """

        async def criar(_: int) -> Amostra:
            return await medir(
                lambda: cliente.post(
                    "/v1/analises", json=payload(), headers={"Idempotency-Key": str(uuid4())}
                )
            )

        base = await rodar(criar, concorrencia=10, total=100)

        # Contencao: consultas de leitura pesadas no mesmo banco, em paralelo com a carga da API.
        parar = asyncio.Event()

        async def contencao() -> None:
            while not parar.is_set():
                async with pool.connection() as conexao:
                    await conexao.execute("SELECT count(*) FROM analise")
                await asyncio.sleep(0)

        rivais = [asyncio.create_task(contencao()) for _ in range(4)]
        try:
            sob_carga = await rodar(criar, concorrencia=10, total=100)
        finally:
            parar.set()
            await asyncio.gather(*rivais, return_exceptions=True)

        print("\n" + tabela([("banco ocioso", base), ("banco sob contencao", sob_carga)]))

        # A propriedade: contencao degrada latencia, e nao corretude.
        assert base.falhas == []
        assert sob_carga.falhas == []


@pytest.fixture(autouse=True)
def _marca_o_horario() -> Any:
    """Imprime quando a rodada aconteceu, para a tabela de referencia poder ser datada."""
    print(f"\n  [{datetime.now(UTC).isoformat(timespec='seconds')}]")
    return None
