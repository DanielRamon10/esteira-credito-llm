"""Metricas Prometheus do servico.

## Onde as metricas moram, e por que nao no dominio

`domain` e `application` nao importam nada daqui. Instrumentar um caso de uso
faria a camada de aplicacao depender do `prometheus_client` — exatamente a
inversao que os ports existem para impedir. As metricas sao coletadas na
**borda**: no middleware HTTP, nas rotas (que ja veem o resultado do caso de uso)
e nos adapters de infraestrutura, que sao clientes de biblioteca de qualquer
forma.

## Cardinalidade: o que NAO e label

Cada combinacao de valores de label cria uma serie temporal, e serie temporal
custa memoria no Prometheus para sempre — nao apenas enquanto o valor aparece.
Por isso ficam **fora** dos labels:

- `analise_id`, `documento_id`, qualquer UUID — cardinalidade ilimitada por
  construcao. Um dia de trafego criaria milhares de series inuteis.
- CPF, nome, valor solicitado — alem de explodir cardinalidade, seria dado
  pessoal num sistema que nao tem controle de acesso de dado pessoal. Metrica
  vaza para dashboard, alerta, e-mail de alerta e print de Slack. **Nao ha dado
  pessoal em label nem em span.**
- caminho HTTP cru. `/v1/analises/8c1f9e...` viraria uma serie por analise; o
  label usa o **template** da rota (`/v1/analises/{analise_id}`).

O que sobra e sempre de dominio fechado: nome de rota, status, modelo, decisao,
motivo de parada, categoria de injecao. Todos com uma lista finita e pequena de
valores possiveis.

Para investigar um caso individual existe log estruturado com `analise_id` e
trace com span — cada ferramenta na sua funcao. Metrica responde "quantos e quao
rapido"; log e trace respondem "qual".

## Buckets: o default do Prometheus nao serve para este sistema

Os buckets padrao terminam em **10s**. Medido neste projeto: a fundamentacao com
RAG leva ~80s, o agente ~80s e a fundamentacao com `llama3.1:8b` chegou a 148s.
Com os buckets padrao, *toda* chamada de LLM cairia no bucket `+Inf` — e
`histogram_quantile` sobre um unico bucket infinito devolve numero sem
significado. O p95 apareceria no dashboard, pareceria correto, e estaria errado.

Por isso ha duas escalas de bucket: uma para HTTP comum e outra, muito mais
longa, para inferencia. E o tipo de detalhe que so aparece depois de medir o
sistema de verdade.
"""

from __future__ import annotations

from plataforma.metricas import BUCKETS_HTTP, BUCKETS_INFERENCIA
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

# Registry proprio em vez do global.
#
# O `REGISTRY` default e um singleton de processo, e a suite cria a aplicacao
# dezenas de vezes. Com metricas no registry global, a segunda criacao levantaria
# `Duplicated timeseries in CollectorRegistry`. Um registry explicito tambem deixa
# claro no `/metrics` que so sai o que este modulo declara — nada de metrica de
# biblioteca entrando por acidente.
REGISTRO = CollectorRegistry()

PREFIXO = "credito"

# `BUCKETS_HTTP` e `BUCKETS_INFERENCIA` vem de `plataforma.metricas`.
#
# Eles foram medidos **aqui**, neste servico, e e justamente por isso que subiram para a
# biblioteca: 80s para o agente e 148s para fundamentacao com llama3.1:8b nao sao numeros
# deste dominio, sao numeros do Ollama nesta maquina. Qualquer servico do monorepo que
# chame o mesmo modelo herda a mesma distribuicao, e mante-los duplicados garantiria que
# um dia dois servicos teriam escalas diferentes sem ninguem ter decidido isso.
#
# `BUCKETS_RETRIEVAL` continua aqui embaixo porque e o oposto: ele descreve a busca
# vetorial **deste** corpus, e nenhum outro servico tem esse componente.

# Retrieval nao chama modelo generativo: e busca vetorial mais BM25.
#
# Medido em regime, com o corpus de 37 trechos: **8 de 9 consultas entre 100ms e
# 250ms**. O bucket de 0,25s e onde a distribuicao vive.
#
# O ultimo bucket vai a 10s por causa de um caso que a propria metrica revelou: a
# **primeira** consulta depois do boot levou ~5,8s, porque o modelo de embedding
# (e5-large, 2,24GB em ONNX) e carregado sob demanda na primeira chamada. Com o
# teto anterior de 5s essa observacao caia em `+Inf`, e ali o
# `histogram_quantile` nao consegue estimar: ele devolve o ultimo bucket finito.
# O painel mostrava "p95 = 5s" — detectando o fenomeno mas incapaz de medi-lo.
#
# O carregamento tardio e deliberado (ver `criar_app`): uma replica que nunca
# recebe consulta de politica nao deve pagar 2,24GB no boot. O que muda agora e
# que o custo dessa escolha esta medido, e nao estimado — 5,8s na primeira
# requisicao de quem chegar primeiro. Se isso passar a incomodar, a correcao e
# aquecer o modelo antes de o `/ready` responder OK, e a decisao passa a ser
# tomada com numero.
BUCKETS_RETRIEVAL = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


# --------------------------------------------------------------------- HTTP

http_requisicoes = Counter(
    f"{PREFIXO}_http_requisicoes_total",
    "Requisicoes HTTP atendidas.",
    labelnames=("metodo", "rota", "status"),
    registry=REGISTRO,
)

http_duracao = Histogram(
    f"{PREFIXO}_http_duracao_segundos",
    "Latencia das requisicoes HTTP.",
    labelnames=("metodo", "rota"),
    buckets=BUCKETS_HTTP,
    registry=REGISTRO,
)

http_em_andamento = Gauge(
    f"{PREFIXO}_http_em_andamento",
    "Requisicoes sendo processadas agora.",
    labelnames=("rota",),
    registry=REGISTRO,
)


# ------------------------------------------------------- Autenticacao (C7)

auth_decisoes = Counter(
    f"{PREFIXO}_auth_decisoes_total",
    "Decisoes de autenticacao, por evento e motivo.",
    # `evento` (aceito/negado) e `motivo` juntos, e nao apenas o motivo. O aceito precisa
    # ser contado para existir denominador: "50 negativas em 10min" nao distingue um cliente
    # recem-integrado com configuracao errada de forca bruta — o que separa os dois e a
    # proporcao sobre o total.
    #
    # `motivo` vem do dominio fechado de `plataforma.autenticacao` (ausente, invalido,
    # expirado, audiencia_incorreta, escopo_insuficiente, ok). Nada de conteudo de token
    # aqui: cardinalidade ilimitada e, pior, credencial vazando para o painel.
    labelnames=("evento", "motivo"),
    registry=REGISTRO,
)


# ---------------------------------------------------------------------- LLM

llm_chamadas = Counter(
    f"{PREFIXO}_llm_chamadas_total",
    "Chamadas a modelo de linguagem.",
    labelnames=("modelo", "operacao", "resultado"),
    registry=REGISTRO,
)

llm_duracao = Histogram(
    f"{PREFIXO}_llm_duracao_segundos",
    "Latencia de chamada a modelo de linguagem.",
    labelnames=("modelo", "operacao"),
    buckets=BUCKETS_INFERENCIA,
    registry=REGISTRO,
)

llm_tokens = Counter(
    f"{PREFIXO}_llm_tokens_total",
    "Tokens consumidos, por direcao.",
    labelnames=("modelo", "direcao"),
    registry=REGISTRO,
)


# ---------------------------------------------------------------------- RAG

retrieval_duracao = Histogram(
    f"{PREFIXO}_retrieval_duracao_segundos",
    "Latencia da busca hibrida no corpus de politicas.",
    buckets=BUCKETS_RETRIEVAL,
    registry=REGISTRO,
)

retrieval_trechos = Histogram(
    f"{PREFIXO}_retrieval_trechos",
    "Quantidade de trechos devolvidos por consulta.",
    buckets=(0, 1, 2, 3, 5, 8, 13, 21),
    registry=REGISTRO,
)

# A metrica de alucinacao prometida na Camada 2.
#
# Um contador com label `estado` em vez de dois contadores separados: a pergunta
# que interessa e a **razao** entre rejeitadas e total, e com um unico nome isso
# e uma divisao direta em PromQL. Com dois nomes, exigiria somar os dois no
# denominador e a consulta erra silenciosamente quando um terceiro estado
# aparecer.
citacoes = Counter(
    f"{PREFIXO}_citacoes_total",
    "Citacoes produzidas pelo modelo, por desfecho da verificacao.",
    labelnames=("estado",),
    registry=REGISTRO,
)


# -------------------------------------------------------------------- Agente

agente_atendimentos = Counter(
    f"{PREFIXO}_agente_atendimentos_total",
    "Atendimentos do agente, por motivo de parada.",
    labelnames=("modelo", "motivo_parada"),
    registry=REGISTRO,
)

agente_duracao = Histogram(
    f"{PREFIXO}_agente_duracao_segundos",
    "Duracao total de um atendimento do agente.",
    labelnames=("modelo",),
    buckets=BUCKETS_INFERENCIA,
    registry=REGISTRO,
)

# Buckets a partir de zero de proposito: **zero passos e o caso saudavel** de
# uma saudacao, e ele responde em 5s contra 80s. A distribuicao de passos e a
# metrica de abstencao, nao apenas de profundidade.
BUCKETS_AGENTE_PASSOS = (0, 1, 2, 3, 4, 6, 8)

agente_passos = Histogram(
    f"{PREFIXO}_agente_passos",
    "Ferramentas executadas por atendimento.",
    buckets=BUCKETS_AGENTE_PASSOS,
    registry=REGISTRO,
)

agente_ferramentas = Counter(
    f"{PREFIXO}_agente_ferramentas_total",
    "Execucoes de ferramenta pelo agente.",
    labelnames=("ferramenta", "resultado"),
    registry=REGISTRO,
)


# ---------------------------------------------------------------------- OCR

ocr_extracoes = Counter(
    f"{PREFIXO}_ocr_extracoes_total",
    "Extracoes de texto por OCR.",
    labelnames=("motor", "resultado"),
    registry=REGISTRO,
)

ocr_confianca = Histogram(
    f"{PREFIXO}_ocr_confianca_pct",
    "Confianca media reportada pelo motor de OCR.",
    buckets=(50, 60, 70, 75, 80, 85, 90, 95, 100),
    registry=REGISTRO,
)

ocr_escalonamentos = Counter(
    f"{PREFIXO}_ocr_escalonamentos_total",
    "Vezes que a cadeia de OCR escalou para um motor mais caro.",
    labelnames=("de", "para"),
    registry=REGISTRO,
)


# ---------------------------------------------------------------------- KYC

# Contador com o desfecho como label, incluindo `circuito_aberto` e `indisponivel`.
#
# A taxa de `indisponivel` sobre o total e a metrica que importa: ela mede quantas
# analises estao indo para revisao humana por causa de uma falha de integracao, e
# nao por causa do caso do cliente. Um numero que sobe aqui e fila de analista
# crescendo por motivo tecnico.
kyc_consultas = Counter(
    f"{PREFIXO}_kyc_consultas_total",
    "Consultas ao servico de KYC, por desfecho.",
    labelnames=("resultado",),
    registry=REGISTRO,
)

# Buckets curtos: a triagem do outro servico e comparacao em memoria, e o timeout do
# cliente e 3s. Reusar os buckets de inferencia (que vao a 320s) desperdicaria toda a
# resolucao onde as chamadas realmente vivem.
kyc_duracao = Histogram(
    f"{PREFIXO}_kyc_duracao_segundos",
    "Latencia da consulta ao servico de KYC.",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 3.0),
    registry=REGISTRO,
)

# Estado do disjuntor como gauge de valor 1 no estado corrente. Permite alertar em
# "circuito aberto por mais de 2 minutos", que e um sintoma diferente de "taxa de
# erro alta" — o circuito aberto ja parou de tentar.
kyc_disjuntor = Gauge(
    f"{PREFIXO}_kyc_disjuntor",
    "Estado do disjuntor do cliente de KYC (1 no estado corrente).",
    labelnames=("estado",),
    registry=REGISTRO,
)


# ---------------------------------------------------------------- Seguranca

# Contador que deve gerar alerta, nao painel bonito: tentativa de injecao em
# documento de credito e indicio de fraude. `superficie` distingue de onde veio
# — documento do cliente, retorno de ferramenta — porque a resposta operacional
# e diferente em cada caso.
injecao_detectada = Counter(
    f"{PREFIXO}_injecao_detectada_total",
    "Padroes de prompt injection detectados em conteudo nao confiavel.",
    labelnames=("superficie", "categoria"),
    registry=REGISTRO,
)


# ----------------------------------------------------------------- Negocio

decisoes = Counter(
    f"{PREFIXO}_decisoes_total",
    "Pareceres emitidos, por decisao e nivel de risco.",
    labelnames=("decisao", "nivel_risco"),
    registry=REGISTRO,
)

score = Histogram(
    f"{PREFIXO}_score",
    "Distribuicao do score de credito calculado.",
    buckets=(200, 350, 450, 500, 600, 700, 800, 900, 1000),
    registry=REGISTRO,
)

comprometimento_renda = Histogram(
    f"{PREFIXO}_comprometimento_renda_pct",
    "Distribuicao do comprometimento de renda apurado.",
    buckets=(10, 20, 30, 40, 50, 60, 80, 100),
    registry=REGISTRO,
)

revisao_humana = Counter(
    f"{PREFIXO}_revisao_humana_total",
    "Casos encaminhados para revisao humana.",
    labelnames=("motivo",),
    registry=REGISTRO,
)


# -------------------------------------------------------------------- Build

info_servico = Gauge(
    f"{PREFIXO}_info",
    "Metadados da instancia. O valor e sempre 1; o que importa sao os labels.",
    labelnames=("versao", "ambiente", "provedor_llm", "modelo_agente"),
    registry=REGISTRO,
)


def registrar_info(versao: str, ambiente: str, provedor_llm: str, modelo_agente: str) -> None:
    """Publica os metadados da instancia como serie de valor 1.

    O padrao `_info` com valor fixo permite juntar versao a qualquer outra
    metrica em PromQL (`... * on(instance) group_left(versao) credito_info`), o
    que responde "a latencia piorou depois do deploy?" sem precisar de anotacao
    manual no dashboard.
    """
    info_servico.labels(
        versao=versao,
        ambiente=ambiente,
        provedor_llm=provedor_llm,
        modelo_agente=modelo_agente,
    ).set(1)


def valor(metrica: Counter | Gauge | Histogram, **labels: str) -> float:
    """Le o valor atual de uma serie — usado nos testes.

    Os testes afirmam **variacao** e nao valor absoluto: as metricas sao
    singletons de processo e a suite roda centenas de requisicoes, entao um
    absoluto seria acoplado a ordem dos testes. Medir o delta e mais honesto e
    reflete como o Prometheus le contador (por taxa, nao por valor).
    """
    total = 0.0
    nome_base = metrica._name

    for metrica_coletada in metrica.collect():
        for amostra in metrica_coletada.samples:
            if labels and not all(amostra.labels.get(k) == v for k, v in labels.items()):
                continue
            # Histogram expoe `_count`, `_sum` e `_bucket`; Counter expoe
            # `_total`. Somar so o total/count mantem o helper util para os dois.
            if amostra.name in (f"{nome_base}_total", f"{nome_base}_count"):
                total += amostra.value
    return total


# --------------------------------------------- Fluxo assincrono (Camada 8)

documentos_recebidos = Counter(
    f"{PREFIXO}_documentos_recebidos_total",
    "Documentos aceitos para extracao, por estado inicial.",
    labelnames=("tipo",),
    registry=REGISTRO,
)

extracoes = Counter(
    f"{PREFIXO}_extracoes_total",
    "Extracoes concluidas pelo trabalhador, por desfecho.",
    # `desfecho` e dominio fechado: aplicada, rejeitada, falhou, ja_aplicada. O ultimo importa
    # tanto quanto os outros — ele mede reentrega, e reentrega crescente indica trabalhador
    # morrendo antes de confirmar, nao problema de OCR.
    labelnames=("desfecho",),
    registry=REGISTRO,
)

fila_espera = Histogram(
    f"{PREFIXO}_fila_espera_segundos",
    "Tempo entre a recepcao do documento e o inicio da extracao.",
    # Buckets ate 320s, os mesmos da inferencia: a fila herda a latencia do OCR, e um documento
    # atras de tres outros espera o tempo dos tres. Com os buckets de HTTP (teto de 10s), tudo
    # cairia em `+Inf` e o p95 seria ficcao — o mesmo erro que a Camada 5 corrigiu.
    buckets=BUCKETS_INFERENCIA,
    registry=REGISTRO,
)
