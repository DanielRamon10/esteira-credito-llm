# Esteira de Crédito Inteligente

Plataforma de análise de crédito com extração documental (OCR), consulta a
políticas internas via RAG e orquestração por agents — construída como
monorepo de microsserviços.

> Projeto de estudo/portfólio. Os dados, políticas e integrações são
> sintéticos; nenhuma informação real de instituição financeira é utilizada.

## Por que este projeto existe

Demonstrar, ponta a ponta, o que uma aplicação transacional com LLM exige em
produção: API segura e escalável, pipeline de inferência, observabilidade,
testes automatizados e infraestrutura como código — sem que o LLM vire uma
caixa-preta que ninguém consegue auditar.

## Arquitetura

```
Praticando_RAG/
├── services/
│   └── credit-analysis/     # Análise de documentos de crédito
├── infra/                   # Terraform, manifests Kubernetes
├── docs/                    # Decisões de arquitetura (ADRs)
└── docker-compose.yml       # Stack local completa
```

Cada serviço segue **arquitetura hexagonal** (ports & adapters):

```
domain/          Regras de negócio puras. Zero dependência de framework.
application/     Casos de uso + ports (interfaces que a app exige do mundo).
infrastructure/  Adapters concretos: banco, LLM, OCR, bureau.
api/             Camada HTTP (FastAPI). Traduz JSON <-> domínio.
```

A regra: **as dependências apontam para dentro**. `domain` não importa nada de
`infrastructure`. É isso que permite testar a lógica de crédito sem subir
banco, e trocar SQLite por Postgres sem tocar em caso de uso.

## Status

| Camada | Escopo | Status |
|---|---|---|
| 1 | Domínio, API REST, scoring, testes | ✅ Concluída |
| 2 | RAG sobre políticas internas (pgvector, híbrido, citações verificadas) | ✅ Concluída |
| 3 | OCR, extração documental e defesa contra prompt injection | ✅ Concluída |
| 4 | Agent LangGraph + tools | ⬜ |
| 5 | Observabilidade (Prometheus/Grafana/tracing) | ⬜ |
| 6 | Terraform, Kubernetes, CI/CD | ⬜ |

## Rodando

```bash
# 0. Tesseract com português (OCR local). Sem ele, a esteira funciona e os
#    endpoints de documento respondem 503 explicando o que falta.
winget install UB-Mannheim.TesseractOCR
#    O pacote não traz o português; baixe por.traineddata do repositório
#    tesseract-ocr/tessdata para C:\Program Files\Tesseract-OCR\tessdata\

# 1. Banco vetorial
docker compose up -d

# 1b. LLM local (opcional, sem chave e sem conta). Sem ele o serviço sobe
#     igual e a geração de texto cai num fake determinístico.
winget install Ollama.Ollama
ollama pull llama3.1:8b

# 2. Dependências
cd services/credit-analysis
uv venv --python 3.12
uv pip install -e ".[dev]"

# 3. Indexar o corpus de políticas (passo de deploy, não de boot)
export CREDIT_POSTGRES_DSN=postgresql://credito:credito_local@localhost:5432/credito
.venv/Scripts/python -m credit_analysis.ingestao --recriar

# 4. Subir a API
.venv/Scripts/python -m credit_analysis
```

Documentação interativa em <http://localhost:8000/docs>.

> Use `python -m credit_analysis`, não `uvicorn ...` direto. No Windows o
> Uvicorn força o `ProactorEventLoop`, sobre o qual o psycopg async não roda —
> o servidor trava em "Waiting for application startup" sem erro. O entrypoint
> resolve isso; veja `infrastructure/event_loop.py`.

Sem `CREDIT_POSTGRES_DSN` a API sobe normalmente e a esteira de crédito
funciona — apenas os endpoints `/v1/politicas` respondem 503 explicando o que
falta.

### Escolha do LLM

`CREDIT_PROVEDOR_LLM` aceita `auto` (padrão), `ollama`, `anthropic` ou `fake`.
No modo automático a ordem é: **Anthropic se houver chave → Ollama se o daemon
responder → fake**. Nenhuma etapa quebra a subida; o adapter escolhido aparece
no log de boot.

Forçar `ollama` ou `anthropic` inverte isso de propósito: se o provedor exigido
não estiver disponível, o serviço **falha ao subir** em vez de virar fake em
silêncio. Num ambiente de verdade, um serviço de crédito respondendo com texto
falso porque o LLM caiu é pior que um serviço fora do ar.

O padrão local é `llama3.1:8b`. A escolha foi medida contra o guardrail real de
citações, não contra uma métrica genérica — a tabela e a conclusão contraintuitiva
("maior nem sempre é melhor nesta tarefa") estão em
`infrastructure/llm/ollama_adapter.py`. O preço do modelo local é latência:
~70s por resposta em CPU.

## Qualidade

```bash
.venv/Scripts/python -m pytest                  # unitários + integração
.venv/Scripts/python -m pytest -m eval          # qualidade do retrieval (baixa 2,2GB)
.venv/Scripts/python -m pytest -m ocr           # acurácia de OCR (exige Tesseract)
.venv/Scripts/python -m ruff check src tests    # lint
.venv/Scripts/python -m mypy                    # tipagem estrita
```

Estado atual: **312 testes** + 20 evals (retrieval e OCR), `mypy --strict` sem
erros em 53 arquivos.

## Segredos

Nenhuma chave é necessária para rodar o projeto — veja a tabela de degradação
abaixo. Quando você configurar uma:

```bash
git config core.hooksPath .githooks   # uma vez por clone
cp services/credit-analysis/.env.example services/credit-analysis/.env
# edite o .env — ele está no .gitignore
```

O hook `pre-commit` bloqueia commit que contenha algo com formato de chave
(`sk-ant-`, `ghp_`, `AKIA`, chave privada PEM). É a defesa mais barata porque
roda **antes** de o commit existir: depois que a chave entra no histórico,
removê-la exige reescrever o histórico, e se já foi para o GitHub considere-a
comprometida — há bots varrendo commits públicos em segundos.

### O que funciona sem chave nenhuma

| Capacidade | Sem `ANTHROPIC_API_KEY` |
|---|---|
| Domínio, scoring, API REST | ✅ completo |
| Retrieval (embeddings locais, ONNX) | ✅ completo |
| OCR (Tesseract local) | ✅ completo |
| Extração, reavaliação de score, segurança | ✅ completo |
| Testes (312 + 20 evals) | ✅ todos passam |
| Geração de texto do parecer | ✅ com Ollama local; sem ele, `LLMFake` |
| Escalonamento de OCR para visão | ⚠️ fora da cadeia |

Isso é requisito de projeto, não acaso: quem clonar o repositório para avaliar
não vai ter uma chave, e o projeto precisa funcionar assim mesmo.

O único ponto que ainda depende da API paga é o OCR por visão, e por escolha:
é escalonamento para documento degradado, com o Tesseract local resolvendo o
caso comum.

## Segurança aplicada a IA

Duas superfícies de conteúdo não confiável, tratadas de forma diferente:

| Fonte | Confiança | Defesa |
|---|---|---|
| Corpus de políticas (Camada 2) | Interna, confiável | Citações verificadas contra os trechos recuperados |
| Documento do cliente (Camada 3) | **Não confiável** | Envelope com delimitador neutralizado + detecção + revisão humana |

A defesa mais forte é arquitetural, não textual: **o valor da renda que alimenta
o score vem de extração por regex sobre o documento, nunca do LLM.** Uma injeção
de prompt pode influenciar o texto que o modelo redige; não pode mudar o número
usado no cálculo.

### Limitação conhecida: o guardrail verifica citações, não prosa

Uma resposta pode ter **todas as citações confirmadas** e ainda assim afirmar
algo falso no texto corrido. Isso foi observado rodando a demo, não suposto: com
`confiavel=True` e três citações literais válidas, o `llama3.1:8b` escreveu que
"a janela de apuração de 6 meses pode ser reduzida para 3 meses se o cliente
tiver vínculo CLT". A POL-005 §2 diz o oposto. O modelo costurou duas políticas
verdadeiras numa conclusão falsa — as citações eram legítimas, a costura não.

Por isso `Fundamentacao.confiavel` significa *"as citações conferem"*, e não
*"o texto está correto"*, e por isso a fundamentação é **insumo** do parecer e
nunca a decisão: quem decide é o `scoring.py`, determinístico e auditável. O LLM
explica a regra; ele não a aplica.

Cobrir a prosa exige outra classe de controle — verificação sentença a sentença
contra os trechos, ou um segundo modelo como juiz — ao custo de outra chamada por
resposta. Fica registrado como limitação em aberto em vez de escondido atrás de
um `confiavel=True` que promete mais do que entrega.

## Serviços planejados

| Serviço | Domínio |
|---|---|
| `credit-analysis` | Análise documental e parecer de crédito |
| `customer-support` | Atendimento com RAG sobre produtos e tarifas |
| `kyc-compliance` | Triagem KYC e normativos regulatórios |
