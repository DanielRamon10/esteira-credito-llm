"""Tracing distribuido com OpenTelemetry.

## O que trace resolve que metrica e log nao resolvem

Metrica responde "o p95 do agente e 80s". Log responde "esta requisicao levou
83s". Nenhum dos dois responde **onde** foram os 83s — e num atendimento com
quatro etapas (decidir, buscar politica, decidir de novo, responder) essa e a
unica pergunta que importa para otimizar.

Neste sistema a resposta ja apareceu no trace: quase todo o tempo esta nas
chamadas ao modelo, e a busca vetorial custa milissegundos. Sem trace, a
suspeita natural seria o banco.

## Sem OTLP configurado, o servico sobe igual

`CREDIT_OTLP_ENDPOINT` vazio desliga o tracing e nada quebra. Observabilidade
que impede a aplicacao de subir e uma dependencia nova em producao, e das piores:
transformaria uma falha no coletor de traces numa indisponibilidade do servico de
credito.

## Amostragem: 100% aqui, e isso e uma escolha

A pratica comum e amostrar 1% a 10% para conter custo. Aqui a razao para nao
amostrar e o perfil do trafego: uma esteira de credito faz **poucas requisicoes
caras** (80s cada), nao milhoes de requisicoes baratas. Descartar 90% dos traces
economizaria pouco e jogaria fora justamente o trace da requisicao que alguem
vai querer investigar. `CREDIT_TRACE_AMOSTRAGEM` existe para o dia em que o
volume mudar essa conta.

## Dado pessoal nao entra em span

Mesma regra dos labels de metrica, pelo mesmo motivo e com uma agravante: span
carrega texto livre com folga, entao a tentacao de anexar "a pergunta do usuario"
ou "o texto extraido do documento" e grande. Nada disso entra. Span recebe
identificador tecnico, contagem, duracao e nome de ferramenta. Um coletor de
traces e um sistema de terceiro com retencao propria e controle de acesso mais
fraco que o do banco de dados.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

NOME_TRACER = "credit-analysis"

_habilitado = False


def configurar_tracing(
    endpoint: str,
    nome_servico: str,
    versao: str,
    ambiente: str,
    amostragem: float = 1.0,
) -> bool:
    """Liga o tracing quando ha endpoint. Devolve se ficou ativo.

    Idempotente: chamar duas vezes (a suite cria a aplicacao muitas vezes) nao
    duplica exportador nem provider.
    """
    global _habilitado

    if not endpoint.strip():
        logger.info("tracing.desabilitado", motivo="CREDIT_OTLP_ENDPOINT vazio")
        return False

    if _habilitado:
        return True

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    recurso = Resource.create(
        {
            "service.name": nome_servico,
            "service.version": versao,
            "deployment.environment": ambiente,
        }
    )

    provider = TracerProvider(
        resource=recurso,
        # ParentBased em volta do ratio: se a requisicao ja chega com decisao de
        # amostragem tomada por quem chamou, ela e respeitada. Sem isso, um trace
        # distribuido fica com buracos — o servico A grava, o B descarta, e o
        # resultado e pior que nao ter trace nenhum.
        sampler=ParentBased(root=TraceIdRatioBased(amostragem)),
    )
    # BatchSpanProcessor e nao Simple: o exportador simples faz uma requisicao
    # HTTP por span, na thread que esta atendendo o cliente.
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"))
    )
    trace.set_tracer_provider(provider)

    _habilitado = True
    logger.info("tracing.habilitado", endpoint=endpoint, amostragem=amostragem)
    return True


def instrumentar_fastapi(app: Any) -> None:
    """Gera span automatico por requisicao HTTP, com a query string removida.

    `/metrics` e `/health` ficam de fora: o Prometheus raspa a cada 15s e o
    Kubernetes sonda a cada poucos segundos, o que encheria o coletor de traces
    identicos e sem valor diagnostico, escondendo os traces que importam.

    O `server_request_hook` existe por uma razao encontrada medindo, nao
    prevendo. A regra "dado pessoal nao entra em span" estava sendo cumprida
    pelos spans **escritos aqui** e violada pelos spans **gerados
    automaticamente**: inspecionando um trace real no Tempo, o atributo
    `http.url` continha `...?q=limite+cdc`. Numa consulta livre a query string e
    texto que o usuario escreveu, e nada impede que ela contenha nome ou CPF.

    E o tipo de vazamento que passa em qualquer revisao de codigo, porque o
    codigo que vaza nao esta no repositorio — esta na biblioteca de
    instrumentacao. So aparece olhando o dado que chegou do outro lado.
    """
    if not _habilitado:
        return

    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(
        app,
        excluded_urls="metrics,health,ready",
        server_request_hook=_remover_query_string,
    )


# Atributos de URL que o instrumentador preenche e que podem carregar a query
# string. Os dois primeiros nomes sao da convencao antiga, os dois ultimos da
# atual — a lista cobre as duas porque a versao do pacote muda qual e usada.
_ATRIBUTOS_DE_URL = ("http.url", "http.target", "url.full", "url.path")


def _remover_query_string(span: Any, scope: dict[str, Any]) -> None:
    """Corta tudo a partir do `?` nos atributos de URL do span.

    Remover em vez de mascarar: um `q=<redacted>` ainda revelaria o tamanho e a
    presenca de cada parametro, e para diagnostico o caminho sozinho basta — a
    rota, o status e a duracao e que dizem onde o tempo foi.

    O caminho continua inteiro, com o UUID da analise. Isso e proposital: em
    trace, ao contrario de metrica, o identificador e justamente o que permite
    cruzar com o log estruturado e reconstruir um caso. UUID nao e dado pessoal;
    a query string livre pode ser.
    """
    if span is None or not getattr(span, "is_recording", lambda: False)():
        return

    atributos = getattr(span, "attributes", None) or {}
    for chave in _ATRIBUTOS_DE_URL:
        valor = atributos.get(chave)
        if isinstance(valor, str) and "?" in valor:
            span.set_attribute(chave, valor.split("?", 1)[0])


@contextmanager
def span(nome: str, **atributos: Any) -> Iterator[None]:
    """Abre um span, ou nao faz nada quando o tracing esta desligado.

    O `no-op` importa: sem ele, cada ponto instrumentado precisaria de um `if`, e
    codigo de negocio cheio de `if tracing_ativo` e pior que codigo sem tracing.
    Aqui quem chama escreve `with span(...)` sempre, e o custo quando desligado e
    um `yield`.
    """
    if not _habilitado:
        yield
        return

    from opentelemetry import trace

    tracer = trace.get_tracer(NOME_TRACER)
    with tracer.start_as_current_span(nome) as atual:
        for chave, valor in atributos.items():
            if valor is not None:
                atual.set_attribute(chave, valor)
        yield


def marcar_erro(excecao: BaseException) -> None:
    """Marca o span corrente como falho, sem interromper nada."""
    if not _habilitado:
        return

    from opentelemetry import trace

    atual = trace.get_current_span()
    atual.record_exception(excecao)
    atual.set_status(trace.Status(trace.StatusCode.ERROR, str(excecao)[:200]))


def desligar_para_teste() -> None:
    """Reseta o estado global — usado pela suite.

    Existe porque `_habilitado` e de processo: um teste que liga o tracing
    contaminaria os seguintes, que passariam a tentar exportar span para um
    coletor inexistente.
    """
    global _habilitado
    _habilitado = False
