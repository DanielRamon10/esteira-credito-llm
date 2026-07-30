# plataforma

Infraestrutura técnica compartilhada entre os serviços do monorepo.

## Por que ela existe agora, e não antes

Durante duas camadas eu resisti a criar esta biblioteca, e o motivo está registrado
nos commits: **extrair antes do segundo consumidor é adivinhar a abstração**. Com um
único usuário, qualquer interface parece boa — a que sobrevive é a que foi moldada
por dois ou três casos reais.

O terceiro serviço (`customer-support`) forçou a decisão. Ele precisaria de BM25,
detecção de prompt injection, logging estruturado e adapter de LLM. Copiar tudo isso
pela terceira vez seria indefensável, e a medição mostrou o tamanho da repetição:

| módulo | pontos de uso | autocontido? |
|---|---|---|
| `seguranca` | 15 | não (dependia de métricas) |
| `bm25` | 5 | sim |
| `logging` | 5 | sim |
| `llm` (Ollama) | 6 | não (dependia de métricas e tracing) |

## O que NÃO entra aqui, e essa é a regra mais importante

**Domínio não é compartilhado.** Nada de `CPF`, `Dinheiro`, `AnaliseCredito` ou
`Triagem`. Cada serviço é um *bounded context* e os dois evoluem por pressões
diferentes: o `kyc-compliance` vai precisar de CNPJ e documento estrangeiro (lista
de sanções internacional não tem CPF), o `credit-analysis` não. Compartilhar o
value object forçaria um a acompanhar o outro, ou a ganhar um parâmetro condicional
— que é o acoplamento contra o qual DDD alerta.

Sim, isso significa que a validação de CPF existe duplicada em dois serviços. É o
preço consciente de manter os contextos independentes, e está anotado nos dois
lugares.

## Regra de dependência: plataforma não conhece Prometheus nem OpenTelemetry

Seria natural a biblioteca compartilhada incrementar contadores direto. Ela não faz
isso, e a razão é que forçaria **todo** consumidor futuro à mesma stack de
observabilidade — um serviço que use OpenTelemetry Metrics em vez de
`prometheus_client`, ou nenhuma das duas, não conseguiria usar a biblioteca sem
arrastar a dependência.

Em vez disso, `plataforma` expõe **ganchos de observação**:

```python
from plataforma import seguranca

# O serviço decide o que medir. A biblioteca só avisa que aconteceu.
seguranca.registrar_observador(
    lambda superficie, categoria: metricas.injecao_detectada.labels(
        superficie=superficie, categoria=categoria
    ).inc()
)
```

O efeito prático: o núcleo de `plataforma` tem **uma** dependência (`structlog`),
contra as 21 do `credit-analysis`. Uma biblioteca compartilhada pesada seria pior que
a duplicação que ela remove.

### A exceção opt-in, e por que ela não quebra a regra

`plataforma.metricas` **importa** `prometheus_client`, e só é instalado por quem pede
o extra `metricas`. A regra era "não forçar todo consumidor à mesma stack" — não
"nunca ter helper de Prometheus".

O que forçou a exceção foi concreto: sem ela, os três serviços teriam cada um a sua
cópia do middleware HTTP, incluindo `template_de_rota` — uma função pequena, sutil e
que **já esteve errada uma vez** (usava `route.path`, que omite o prefixo `/v1` e
somaria `/v1` e um futuro `/v2` na mesma série temporal). Triplicar uma lógica com
esse histórico é exatamente o que a extração serviu para evitar.

O núcleo continua limpo, e o teste do CI que verifica isso passou a distinguir os dois
casos em vez de proibir tudo.

## Módulos

| módulo | o que é | por que compartilhar |
|---|---|---|
| `logging` | structlog em JSON com chaves estáveis | não há razão para dois serviços formatarem log diferente |
| `seguranca` | envelope + detecção de prompt injection | **defesa não deve divergir**: um serviço com detecção mais fraca é a porta de entrada |
| `bm25` | índice lexical Okapi BM25, sem dependência externa | mesmo algoritmo, corpora diferentes |
| `llm` | adapter Ollama e fake determinístico | escolha de modelo medida uma vez, aproveitada por todos |
| `metricas` | métricas HTTP e buckets medidos (**opt-in**) | `template_de_rota` é sutil e já esteve errada; buckets vêm de medição |

O `seguranca` é o caso mais forte. Se cada serviço tivesse a própria lista de
padrões, o mais desatualizado passaria a ser o vetor de ataque — e ninguém
perceberia, porque cada um pareceria protegido isoladamente.
