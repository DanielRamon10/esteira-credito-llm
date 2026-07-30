"""Metricas HTTP compartilhadas — modulo **opcional**.

## Por que este modulo existe, e por que ele nao viola a regra da biblioteca

O README diz que `plataforma` nao conhece Prometheus. Isto continua valendo para o
**nucleo** (`logging`, `seguranca`, `bm25`, `llm`), e a razao segue de pe: um servico
que use OpenTelemetry Metrics, ou nenhuma metrica, nao deve ser obrigado a instalar
`prometheus_client` para poder usar deteccao de injecao.

Mas a regra era "nao forcar todo consumidor a mesma stack" — nao "nunca ter helper de
Prometheus". Este modulo e opt-in: so importa quem instalou o extra `metricas`, e o
nucleo continua sem tocar nele.

O que forcou a decisao foi concreto. Sem este modulo, os tres servicos teriam a
propria copia do middleware HTTP, incluindo `template_de_rota` — uma funcao pequena,
sutil e que **ja esteve errada uma vez**: a versao original usava `route.path` do
FastAPI, que devolve `/analises/{id}` **sem o prefixo `/v1`**, o que no dia em que
existisse um `/v2` somaria as duas versoes na mesma serie temporal sem nada indicar a
mistura. Copiar tres vezes uma logica com esse historico e o oposto do que a extracao
da biblioteca serviu para evitar.

## O que fica de fora

As metricas de **dominio** de cada servico. `credito_decisoes_total`,
`suporte_vazamentos_total`, `kyc_triagens_total` — nada disso mora aqui, porque o
nome, os labels e os buckets sao decisao do dominio de cada um. O que se compartilha
e a mecanica de HTTP, que e identica por definicao.
"""

from __future__ import annotations

from collections.abc import Mapping

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

# Buckets de latencia HTTP. Terminam em 10s porque uma requisicao REST que passa disso
# e um incidente, nao um ponto de distribuicao.
BUCKETS_HTTP = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

# Buckets de inferencia, compartilhados porque a medicao que os justifica vale para
# qualquer servico do monorepo que chame LLM local.
#
# Vao a 320s porque o medido em CPU foi 80s (agente) e ate 148s (fundamentacao com
# llama3.1:8b). Com os buckets padrao do Prometheus, que terminam em 10s, **toda**
# chamada de modelo cairia em `+Inf` — e `histogram_quantile` sobre um unico bucket
# infinito devolve numero sem significado. O p95 apareceria no painel e estaria errado.
BUCKETS_INFERENCIA = (0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 40.0, 80.0, 160.0, 320.0)


def criar_registro() -> CollectorRegistry:
    """Registry proprio, em vez do global.

    O `REGISTRY` default e um singleton de processo, e uma suite que cria a aplicacao
    dezenas de vezes levantaria `Duplicated timeseries` na segunda criacao. Um registry
    explicito tambem deixa claro no `/metrics` que so sai o que o servico declara.
    """
    return CollectorRegistry()


def template_de_rota(caminho: str, parametros: Mapping[str, object] | None) -> str:
    """Reconstroi o template da rota a partir do caminho concreto.

    `/v1/analises/8c1f9e.../documentos` como label criaria uma serie temporal por
    analise, e serie temporal no Prometheus custa memoria **para sempre** — nao apenas
    enquanto o valor aparece.

    ## Por que reconstruir em vez de usar `route.path`

    Seria a forma obvia, e esta errada. Medido: com
    `include_router(router, prefix="/v1")`, o `route.path` da rota resultante e
    `/analises/{analise_id}` — **sem o `/v1`**. O prefixo do `include_router` nao entra
    naquele atributo.

    A consequencia seria silenciosa: no dia em que existisse um `/v2`, as duas versoes
    da API cairiam na mesma serie e o painel mostraria latencia somada sem nada
    indicando a mistura.

    A troca e por **segmento inteiro**. Trocar por substring quebraria um caminho em
    que o valor do parametro aparece tambem em outro pedaco da URL.
    """
    if not parametros:
        # Rota sem parametro: o caminho ja e o template, e a cardinalidade e limitada
        # pelo numero de rotas declaradas.
        return caminho

    substituicoes = {str(valor): f"{{{nome}}}" for nome, valor in parametros.items()}
    return "/".join(substituicoes.get(segmento, segmento) for segmento in caminho.split("/"))


ROTA_DESCONHECIDA = "desconhecida"


def rotulo_de_rota(
    caminho: str, parametros: Mapping[str, object] | None, casou_com_rota: bool
) -> str:
    """`template_de_rota`, mais a defesa contra varredura de URL.

    Este e o ponto de entrada que os servicos devem usar. O `template_de_rota` cru fica
    exposto porque e a parte pura e testavel, mas usar so ele deixa um buraco.

    ## Por que a requisicao sem rota casada nao pode virar o caminho pedido

    Sem esta funcao, `/wp-admin`, `/.env`, `/api/v2/usuarios` e cada variacao que um
    scanner tentar viram **uma serie temporal nova cada**. Como serie no Prometheus custa
    memoria para sempre — nao apenas enquanto o valor aparece — qualquer varredura
    automatizada consegue inflar a memoria do Prometheus de fora para dentro, sem
    autenticacao e sem passar por nenhuma rota da aplicacao. E o unico caminho de
    cardinalidade que um atacante controla diretamente.

    Todas as requisicoes nao roteadas colapsam em `desconhecida`, que continua sendo
    informacao util: um salto nessa serie **e** o sinal de que ha varredura acontecendo.

    ## Por que o parametro e um `bool` em vez do `Request`

    Receber o objeto do Starlette obrigaria `plataforma` a depender de FastAPI, e a
    biblioteca tem hoje uma unica dependencia no nucleo. O servico extrai
    `request.scope.get("route") is not None` e passa o resultado — a informacao que
    importa, sem o acoplamento de framework.
    """
    if not casou_com_rota:
        return ROTA_DESCONHECIDA
    return template_de_rota(caminho, parametros)


class MetricasHTTP:
    """Contadores e histograma de requisicao, com os nomes prefixados pelo servico.

    Cada servico tem o proprio prefixo (`credito_`, `kyc_`, `suporte_`) porque as
    series convivem no mesmo Prometheus e um nome unico nao permitiria distinguir a
    latencia de dois servicos sem depender de label de job.
    """

    def __init__(self, registro: CollectorRegistry, prefixo: str) -> None:
        self.requisicoes = Counter(
            f"{prefixo}_http_requisicoes_total",
            "Requisicoes HTTP atendidas.",
            labelnames=("metodo", "rota", "status"),
            registry=registro,
        )
        self.duracao = Histogram(
            f"{prefixo}_http_duracao_segundos",
            "Latencia das requisicoes HTTP.",
            labelnames=("metodo", "rota"),
            buckets=BUCKETS_HTTP,
            registry=registro,
        )
        self.em_andamento = Gauge(
            f"{prefixo}_http_em_andamento",
            "Requisicoes sendo processadas agora.",
            registry=registro,
        )
        self.info = Gauge(
            f"{prefixo}_info",
            "Metadados da instancia. O valor e sempre 1; o que importa sao os labels.",
            labelnames=("versao", "ambiente"),
            registry=registro,
        )

    def registrar(self, metodo: str, rota: str, status: int, duracao: float) -> None:
        """Registra uma requisicao concluida."""
        self.duracao.labels(metodo=metodo, rota=rota).observe(duracao)
        self.requisicoes.labels(metodo=metodo, rota=rota, status=str(status)).inc()

    def publicar_info(self, versao: str, ambiente: str) -> None:
        """Publica os metadados como serie de valor 1.

        O padrao `_info` com valor fixo permite juntar versao a qualquer outra metrica
        em PromQL, o que responde "a latencia piorou depois do deploy?" sem anotacao
        manual no dashboard.
        """
        self.info.labels(versao=versao, ambiente=ambiente).set(1)
