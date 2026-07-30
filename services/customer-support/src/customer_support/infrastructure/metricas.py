"""Metricas do customer-support.

## A metrica que este servico tem e nenhum outro tem

`suporte_vazamentos_bloqueados_total{categoria}` conta quantas vezes o guard de
divulgacao descartou a prosa do modelo. E o equivalente, aqui, da taxa de citacao
rejeitada do `credit-analysis`: um numero que sobe significa que o modelo passou a
produzir conteudo interno, e as causas possiveis sao poucas e acionaveis — mudanca de
modelo, mudanca de prompt, ou um artigo publico que passou a citar numero interno.

Sem essa metrica o guard funcionaria em silencio e ninguem saberia que ele esta
trabalhando mais que antes.

`suporte_atendimentos_total{intencao,origem}` cruza duas dimensoes de proposito. A
`origem` diz se a resposta veio do modelo, do artigo ou de roteiro fixo, e a proporcao
entre elas e um sinal de saude: `artigo` subindo sem vazamento subindo significa Ollama
indisponivel; `roteiro` subindo em `duvida_produto` significa que a base nao esta
respondendo o que perguntam.
"""

from __future__ import annotations

from plataforma.metricas import MetricasHTTP, criar_registro
from prometheus_client import Counter, Histogram

REGISTRO = criar_registro()
PREFIXO = "suporte"

http = MetricasHTTP(REGISTRO, PREFIXO)

atendimentos = Counter(
    f"{PREFIXO}_atendimentos_total",
    "Atendimentos concluidos, por intencao e origem da resposta.",
    labelnames=("intencao", "origem"),
    registry=REGISTRO,
)

# A metrica de guardrail deste servico.
vazamentos_bloqueados = Counter(
    f"{PREFIXO}_vazamentos_bloqueados_total",
    "Vezes que a fronteira de divulgacao descartou a resposta, por categoria.",
    labelnames=("categoria",),
    registry=REGISTRO,
)

injecao_detectada = Counter(
    f"{PREFIXO}_injecao_detectada_total",
    "Padroes de injecao na mensagem do proprio cliente, por categoria.",
    labelnames=("categoria",),
    registry=REGISTRO,
)

encaminhamentos = Counter(
    f"{PREFIXO}_encaminhamentos_total",
    "Casos encaminhados a humano, por motivo.",
    labelnames=("motivo",),
    registry=REGISTRO,
)

# Duas escalas num unico histograma nao funcionaria: resposta de roteiro sai em
# microssegundos e resposta do modelo em dezenas de segundos. O label `origem` separa
# as duas distribuicoes no mesmo nome de metrica.
duracao = Histogram(
    f"{PREFIXO}_atendimento_duracao_segundos",
    "Duracao de um atendimento, por origem da resposta.",
    labelnames=("origem",),
    buckets=(0.001, 0.01, 0.1, 0.5, 1.0, 5.0, 15.0, 30.0, 60.0, 120.0),
    registry=REGISTRO,
)

artigos_recuperados = Histogram(
    f"{PREFIXO}_artigos_recuperados",
    "Artigos publicos devolvidos pela busca.",
    buckets=(0, 1, 2, 3, 5),
    registry=REGISTRO,
)

auth_decisoes = Counter(
    f"{PREFIXO}_auth_decisoes_total",
    "Decisoes de autenticacao, por evento e motivo.",
    # O `aceito` e contado junto com as negativas, e o denominador e o ponto: "50 negativas em
    # 10 minutos" nao distingue um cliente recem-integrado com configuracao errada de forca
    # bruta. O que separa os dois e a proporcao sobre o total.
    #
    # `motivo` vem do dominio fechado de `plataforma.autenticacao`. Nada de conteudo de token
    # aqui: seria cardinalidade ilimitada e, pior, credencial vazando para o painel.
    labelnames=("evento", "motivo"),
    registry=REGISTRO,
)
