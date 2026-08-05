"""Driver de carga: N clientes concorrentes contra a API, com percentis.

## Por que um driver proprio, e nao k6 ou locust

As duas ferramentas sao melhores que isto para carga sustentada, e nenhuma das duas serve bem ao que
esta camada precisa medir.

O que se quer aqui e um **experimento comparativo**: a mesma carga na API com o trabalhador parado e
com ele drenando uma fila, para responder se a separacao de processos entrega o que o manifesto dele
afirma. Isso exige orquestrar dois lados no mesmo roteiro — parar o trabalhador, enfileirar backlog,
soltar, medir a API durante a drenagem.

Somando: o cliente precisa falar o contrato de autenticacao (token RS256 do emissor local) e o de
idempotencia (chave nova por pedido). Em Python isso e reuso do que a suite ja tem; em k6
exigiria uma biblioteca de JWT no runtime dele.

Para carga sustentada em CI, k6 e a escolha certa e isto nao substitui — o que este arquivo faz e
uma medicao pontual, versionada, no espirito de `tests/eval`.

## Por que os numeros nao viram piso de teste

A licao esta em `tests/eval/test_ocr_qualidade.py`: piso numerico sobre medicao dependente de
ambiente mede o ambiente. Latencia depende de CPU, disco, do que mais roda na maquina e de estar num
container ou nao — um `p95 < 200ms` passaria aqui e falharia no runner compartilhado do GitHub, e a
reacao seria afrouxar o numero ate ele nao significar nada.

Entao: os numeros vao para a tabela de referencia no cabecalho de `test_carga.py`, e as
**assercoes** sao propriedades que nao dependem de velocidade — nenhuma requisicao falha, nenhuma
analise duplicada, conflito de versao e sempre transitorio.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass(frozen=True, slots=True)
class Amostra:
    """Uma requisicao: quanto levou e o que respondeu."""

    duracao_ms: float
    status: int

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


@dataclass(slots=True)
class Resultado:
    """Percentis e vazao de uma rodada.

    ## Percentil e nao media

    Media esconde a cauda, e a cauda e o que o cliente sente: com 100 requisicoes de 10ms e uma de
    2s, a media da 30ms e alguem esperou dois segundos. p95 e p99 respondem "quao ruim fica para os
    azarados", que e a pergunta de quem opera.

    ## `sucessos` separado de `total`

    Uma rodada rapida com metade das requisicoes falhando pareceria otima na latencia — erro
    responde rapido. Sem o par, um pool esgotado apareceria como melhora de p95.
    """

    amostras: list[Amostra] = field(default_factory=list)
    duracao_total_s: float = 0.0

    @property
    def total(self) -> int:
        return len(self.amostras)

    @property
    def sucessos(self) -> int:
        return sum(1 for a in self.amostras if a.ok)

    @property
    def falhas(self) -> list[int]:
        """Codigos das respostas que nao foram 2xx, para o erro aparecer com o codigo dele."""
        return sorted({a.status for a in self.amostras if not a.ok})

    @property
    def vazao_por_s(self) -> float:
        return self.total / self.duracao_total_s if self.duracao_total_s > 0 else 0.0

    def percentil(self, p: float) -> float:
        if not self.amostras:
            return 0.0
        ordenadas = sorted(a.duracao_ms for a in self.amostras)
        # `method="inclusive"` para o p95 de amostras pequenas nao virar o maximo: com 20 amostras,
        # o metodo exclusivo devolve o ultimo valor e o percentil deixa de discriminar.
        return float(
            statistics.quantiles(ordenadas, n=100, method="inclusive")[int(p) - 1]
            if len(ordenadas) > 1
            else ordenadas[0]
        )

    def resumo(self) -> str:
        return (
            f"{self.total} req em {self.duracao_total_s:.2f}s "
            f"({self.vazao_por_s:.1f} req/s) | "
            f"p50 {self.percentil(50):.0f}ms  p95 {self.percentil(95):.0f}ms  "
            f"p99 {self.percentil(99):.0f}ms | "
            f"sucessos {self.sucessos}/{self.total}"
            + (f" | falhas: {self.falhas}" if self.falhas else "")
        )


async def rodar(
    acao: Callable[[int], Awaitable[Amostra]],
    *,
    concorrencia: int,
    total: int,
) -> Resultado:
    """Dispara `total` chamadas de `acao`, com no maximo `concorrencia` simultaneas.

    ## Semaforo e nao `gather` de tudo

    `gather` com 500 corrotinas abre 500 conexoes de uma vez e mede o colapso do cliente, nao o do
    servidor. O semaforo mantem a concorrencia **no valor declarado**, que e o parametro do
    experimento — sem ele, "concorrencia 50" seria uma intencao e nao um fato.

    ## O relogio comeca depois de as tarefas existirem

    Criar 500 tarefas leva tempo mensuravel, e incluir isso na duracao inflaria a vazao para baixo
    por um custo que e do driver.
    """
    limite = asyncio.Semaphore(concorrencia)

    async def uma(indice: int) -> Amostra:
        async with limite:
            return await acao(indice)

    tarefas = [asyncio.create_task(uma(i)) for i in range(total)]
    inicio = time.perf_counter()
    amostras = await asyncio.gather(*tarefas)
    decorrido = time.perf_counter() - inicio

    return Resultado(amostras=list(amostras), duracao_total_s=decorrido)


async def medir(chamada: Callable[[], Awaitable[Any]]) -> Amostra:
    """Cronometra uma chamada HTTP e extrai o status, tratando falha de transporte como 0.

    Status 0 e nao excecao: uma conexao recusada e **dado do experimento** — e o sintoma de pool
    esgotado ou de servidor derrubado —, e uma excecao interromperia a rodada e perderia as
    amostras das outras requisicoes.

    ## `httpx.HTTPError` e nao `Exception`

    `except Exception` foi a primeira versao, e o `BLE001` do ruff reclamou com razao. A diferenca
    nao e estilo: falha de transporte e dado, e `KeyError` num atributo do driver e defeito. Com o
    `except` cego, um bug meu apareceria como "status 0" numa tabela de latencia — o experimento
    reportaria colapso do servidor onde havia erro de digitacao.

    `httpx.HTTPError` cobre o que interessa (conexao recusada, timeout de leitura, pool esgotado no
    cliente) e deixa o resto subir.
    """
    inicio = time.perf_counter()
    try:
        resposta = await chamada()
        status = int(resposta.status_code)
    except httpx.HTTPError:
        status = 0
    return Amostra(duracao_ms=(time.perf_counter() - inicio) * 1000, status=status)


def tabela(linhas: Sequence[tuple[str, Resultado]]) -> str:
    """Formata rodadas para colar na tabela de referencia, com o mesmo shape do eval de OCR."""
    largura = max(len(nome) for nome, _ in linhas)
    return "\n".join(f"    {nome:<{largura}}  {resultado.resumo()}" for nome, resultado in linhas)
