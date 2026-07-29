# credit-analysis

Microsserviço de análise de crédito: recebe uma proposta, avalia o risco e
devolve um parecer **explicável** — com o porquê de cada ponto do score.

## O domínio

Um cliente pede R$ 45.000 em 36x. O serviço precisa responder: aprova, nega ou
manda para análise manual? E, principalmente, **por quê**.

A resposta considera:

- **Comprometimento de renda** — quanto da renda a parcela consome (peso 40%)
- **Divergência de renda** — o que foi declarado bate com o comprovado? (20%)
- **Histórico bancário** — profundidade do relacionamento (15%)
- **Restrição cadastral** — veto duro quando presente (15%)
- **Perfil demográfico** — peso baixo de propósito, por risco de viés (10%)

Dois vetos independem do score: restrição cadastral ativa e parcela acima de
50% da renda. Nenhuma combinação de fatores bons compensa esses dois.

### Por que score explicável e não um modelo caixa-preta

Um gradient boosting daria AUC melhor. Mas a Resolução CMN 4.658 e o art. 20
da LGPD dão ao cliente o direito de pedir revisão de decisão automatizada — e
"o modelo disse não" não é justificativa. Aqui cada fator carrega sua própria
frase, e o parecer é a soma delas.

## Camadas

```
domain/
  value_objects.py   CPF (com DV), Dinheiro (Decimal), Percentual
  entities.py        Solicitante, PropostaCredito, AnaliseCredito (agregado)
  scoring.py         Motor de score — NumPy, puro, sem I/O
  extrato.py         Análise de extrato bancário — Pandas
  politica.py        Trecho, referência, citação, fundamentação
  documento.py       Imagem, resultado de OCR, campos extraídos
application/
  ports.py           Interfaces (Protocol) que a aplicação exige
  use_cases/         Orquestração. Não calcula nada.
infrastructure/
  repositories/      Adapter em memória (Postgres vem na Camada 6)
  bureau.py          Stub determinístico do bureau de crédito
  seguranca.py       Envelope e detecção de prompt injection
  rag/               BM25 + denso + RRF, embeddings ONNX, pgvector
  llm/               Adapters Ollama (local) e Anthropic, mesmo port
  ocr/               Pré-processamento, Tesseract, visão, escalonamento
api/
  app.py             Application factory + middleware de correlação
  schemas.py         Contrato HTTP, separado das entidades
  errors.py          Erro de domínio -> status HTTP
```

## Extração documental (Camada 3)

**Não roda OCR quando não precisa.** PDF gerado por sistema tem camada de texto;
extrair direto é exato, instantâneo e sem risco de trocar 8 por B. OCR entra só
em scan e imagem.

**A coluna de saldo é checksum da coluna de valor.** Num extrato,
`saldo - saldo_anterior` confirma o valor lido *e* dá a direção do lançamento
sem depender de sufixo C/D. Quando nem o saldo nem o sufixo resolvem, a linha é
**rejeitada** — assumir crédito na dúvida infla a renda, que é a direção que uma
esteira de crédito não pode errar.

**Escalonamento decide por campos, não por confiança média.** A medição
(`tests/eval/test_ocr_qualidade.py`) mostrou os dois erros de um limiar global:

| Caso | Confiança | Realidade |
|---|---|---|
| Holerite em baixa resolução | 87,8% (acima do limiar) | perdeu o CPF — falso positivo |
| Extrato limpo | 83,9% (abaixo do limiar) | 23 de 24 lançamentos — falso negativo |

Tabela densa de números monoespacados recebe score por palavra mais baixo que
prosa. Por isso a cadeia pergunta "os campos obrigatórios saíram?" e usa a
confiança como sinal secundário.

## Decisões que valem explicar

**`Decimal`, nunca `float`, para dinheiro.** `0.1 + 0.2 != 0.3` em ponto
flutuante. Numa esteira de crédito isso vira divergência de centavos entre o
parecer e o contrato.

**CPF mascarado em toda saída e todo log.** `***.982.247-**`. A resposta da API
acaba em log de proxy, APM e navegador do atendente — nenhum é lugar de dado
pessoal completo.

**`/health` e `/ready` separados.** O Kubernetes faz duas perguntas diferentes:
"reinicio o pod?" e "mando tráfego?". Colapsar as duas causa restart loop
quando uma dependência fica lenta.

**Máquina de estados como dado, não como `if`.** As transições válidas vivem
num dicionário — inspecionável e testável de uma vez, em vez de espalhadas.

**`Protocol` em vez de `ABC` nos ports.** O adapter não herda nada da camada de
aplicação, então a infraestrutura não vira dependência do que deveria ser
independente dela.

## API

| Método | Rota | Descrição |
|---|---|---|
| `POST` | `/v1/analises` | Submete uma análise, devolve o parecer preliminar |
| `GET` | `/v1/analises/{id}` | Consulta uma análise |
| `GET` | `/v1/analises` | Lista paginada |
| `POST` | `/v1/analises/{id}/documentos` | Envia documento, extrai e **reavalia o score** |
| `GET` | `/v1/politicas/buscar` | Retrieval puro no corpus, sem LLM |
| `POST` | `/v1/politicas/consultar` | Resposta fundamentada com citações verificadas |
| `GET` | `/health` | Liveness probe |
| `GET` | `/ready` | Readiness probe |

### Fluxo com comprovação de renda

O parecer inicial usa a renda **declarada**. Quando o documento chega, a análise
é reaberta (`reabrir_para_reavaliacao`, com contador para auditoria) e o score é
recalculado com a renda **comprovada**. Exemplo real medido:

```
declarada R$ 12.000  ->  score 632, comprometimento 14,69%
holerite lido        ->  renda comprovada R$ 7.262,14 (Tesseract, 89,8%)
reavaliado           ->  score 530, comprometimento 24,27%
justificativa        ->  "Divergência de 39,5% entre renda declarada e comprovada"
```

Toda resposta carrega `X-Request-ID` (gerado se o cliente não mandar), e o
mesmo id aparece em cada linha de log da requisição.

### Exemplo

```bash
curl -X POST http://localhost:8000/v1/analises \
  -H 'Content-Type: application/json' \
  -d '{
    "solicitante": {
      "nome": "Maria Oliveira Santos",
      "cpf": "529.982.247-25",
      "data_nascimento": "1990-05-14T00:00:00Z",
      "renda_mensal_declarada": "8500.00"
    },
    "proposta": {
      "valor_solicitado": "45000.00",
      "prazo_meses": 36,
      "taxa_juros_mensal": "1.99"
    },
    "renda_comprovada": "8200.00",
    "meses_historico_bancario": 24
  }'
```

## Erros

Formato único para todo erro, com `codigo` estável para o cliente tratar
programaticamente:

```json
{ "codigo": "analise_nao_encontrada", "mensagem": "Analise ... nao encontrada" }
```

| Código | HTTP |
|---|---|
| `payload_invalido` | 422 |
| `valor_invalido` | 422 |
| `dados_insuficientes` | 422 |
| `transicao_invalida` | 409 |
| `analise_nao_encontrada` | 404 |
| `erro_interno` | 500 |

Mensagem de exceção nunca vai para o cliente — vaza caminho de arquivo e às
vezes credencial. O stack completo fica no log.

## Testes

```bash
.venv/Scripts/python -m pytest              # tudo
.venv/Scripts/python -m pytest tests/unit   # só unitários
.venv/Scripts/python -m pytest -m integration
```

Os unitários testam **comportamento de negócio**, não a fórmula do score.
"Restrição cadastral nega independentemente do score" continua verdade depois
de uma recalibragem de pesos; "esse perfil pontua 743" não.
