"""Metricas do kyc-compliance.

A mecanica de HTTP vem de `plataforma.metricas`; o que esta aqui e **dominio**, e por
isso nao foi compartilhado: nome, label e bucket sao decisao de cada servico.

## A metrica que mais importa neste servico

`kyc_triagens_total{decisao}` responde a pergunta de conformidade — quantos casos
foram reprovados, quantos foram para revisao — mas a que exige atencao operacional e
**a proporcao de `revisao_manual`**. Ela mede fila de analista, e uma fila que cresce
sem o volume crescer significa que o casamento de nomes ficou mais frouxo (ou que a
lista mudou de formato). E o equivalente, aqui, da taxa de citacao rejeitada do
`credit-analysis`.

`kyc_correspondencias_total{nivel}` desce um nivel: separa o que foi casamento forte
do que foi parcial. Um deslocamento de `forte` para `parcial` sem mudanca de codigo
indica lista com nomes mais longos ou mais abreviados que antes.
"""

from __future__ import annotations

from plataforma.metricas import MetricasHTTP, criar_registro
from prometheus_client import Counter, Histogram

REGISTRO = criar_registro()
PREFIXO = "kyc"

http = MetricasHTTP(REGISTRO, PREFIXO)

triagens = Counter(
    f"{PREFIXO}_triagens_total",
    "Triagens concluidas, por decisao e nivel de risco.",
    labelnames=("decisao", "nivel_risco"),
    registry=REGISTRO,
)

correspondencias = Counter(
    f"{PREFIXO}_correspondencias_total",
    "Correspondencias encontradas, por nivel e tipo de lista.",
    labelnames=("nivel", "tipo_lista"),
    registry=REGISTRO,
)

# Buckets curtos: a triagem e comparacao em memoria sobre alguns milhares de entradas.
# Reusar os buckets de inferencia desperdicaria toda a resolucao onde as chamadas
# realmente vivem.
duracao = Histogram(
    f"{PREFIXO}_triagem_duracao_segundos",
    "Duracao de uma triagem completa.",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
    registry=REGISTRO,
)

entradas_avaliadas = Histogram(
    f"{PREFIXO}_entradas_avaliadas",
    "Quantas entradas de lista foram comparadas por triagem.",
    buckets=(10, 50, 100, 500, 1000, 5000, 10000),
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
