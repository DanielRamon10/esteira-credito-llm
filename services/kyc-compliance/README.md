# kyc-compliance

Triagem de clientes contra listas restritivas — PEP, sanções e mídia negativa —
com decisão **determinística e explicável**.

> As listas versionadas aqui são **sintéticas**. Nenhum nome é de pessoa real; ver
> `dados/listas/README.md`.

## O domínio

Antes de conceder crédito, a instituição precisa saber se a pessoa consta em lista
restritiva. A pergunta parece simples e não é: a pessoa se cadastra como "Jose da
Silva Junior", a lista tem "JOSE DA SILVA JR." e o cartório registrou "José Da
Silva Júnior".

Os dois erros possíveis custam coisas diferentes, e essa assimetria decide o
desenho inteiro:

- **Falso negativo** (não acusar quem está na lista) é violação regulatória. A
  Circular BCB 3.978 exige o monitoramento; deixar passar gera multa e termo de
  compromisso.
- **Falso positivo** custa tempo de analista. Caro, mas recuperável.

O limiar pende para sensibilidade. Mas "pender" não é "ignorar precisão": um
sistema que acusa 40% da base treina o analista a aprovar sem ler — e aí o falso
negativo volta pela porta dos fundos.

### PEP não é impedimento

A confusão mais comum desta área. Pessoa Exposta Politicamente exige **diligência
reforçada e aprovação por alçada superior** (Circular BCB 3.978 art. 27), não
recusa. Tratar PEP como bloqueio negaria crédito a milhares de servidores públicos
sem base legal — por isso existe `APROVADO_COM_DILIGENCIA`, e `aprovado` é `true`
nesse caso.

Sanção com casamento forte é o **único** veto duro.

## Por que não há LLM aqui

Não é economia. São duas razões:

**Nome próprio não tem significado a aproximar.** Modelo de embedding aproxima
sentido, e é isso que o torna errado para este problema: "Silva" e "Souza" ficam
vizinhos no espaço vetorial por serem ambos sobrenomes brasileiros comuns —
exatamente o oposto do que a triagem precisa. Casamento de nome é problema
**lexical**: ordem de token, abreviação, acento, erro de digitação.

**Decisão de conformidade precisa ser idêntica hoje e em seis meses.** Um score de
cosseno não explica nada a um regulador; este serviço devolve *quais tokens
casaram e quais faltaram*.

O contraste com o `credit-analysis` é proposital: lá o LLM redige e o motor decide;
aqui não há o que redigir.

## O algoritmo, e os três defeitos que a medição encontrou

Três sinais combinados: cobertura de token (aceitando abreviação e erro),
similaridade de caractere sobre o nome inteiro, e penalidade por sobrenome
distintivo ausente.

**"Maria Silva" vs "Mario Silva" pontuava 0,968.** Nível praticamente idêntico — o
pior lugar possível para um falso positivo, porque o analista confia e aprova. A
causa era tratar toda distância de edição 1 como equivalente. Não são o mesmo
risco:

| tipo de edição | exemplo | risco | regra |
|---|---|---|---|
| transposição | SILVA / SLIVA | assinatura de digitação | sempre aceita |
| inserção/remoção | RODRIGUES / RODRIGES | digitação em token longo | aceita a partir de 6 caracteres |
| substituição na última letra | MARIA / MARIO | **marca gênero em português** | nunca aceita |

Separados, o par caiu para 0,463.

**"E" como partícula anulava a regra de inicial abreviada.** "CARLOS E. LIMA"
perdia o "E" na tokenização, o "EDUARDO" do nome consultado ficava sem par, e um
casamento legítimo caía de 0,93 para 0,690 — *abaixo* de um par que não deveria
casar.

**CPF idêntico com nome diferente gerava veto automático.** O raciocínio original
("documento é identificador mais forte que nome") estava incompleto. CPF igual com
nome compatível é identificação dupla; CPF igual com nome **sem nenhuma palavra em
comum** é, mais provavelmente, erro de digitação no cadastro — e um dígito errado
num arquivo público não pode negar crédito sem revisão humana. Hoje o primeiro caso
é `EXATA` e o segundo é `PARCIAL`, que leva a revisão manual. A flag `cpf_confere`
continua visível: nenhum sinal se perde, só a automação do veto.

### Os limiares saem da medição

Sobre 16 pares (8 da mesma pessoa escritos de formas diferentes, 8 de pessoas
distintas), depois das correções:

```
mesma pessoa       score mínimo   0,934
pessoas distintas  score máximo   0,703
```

Faixa vazia de 0,23. `LIMIAR_FORTE = 0,85` fica no **meio** dela — não encostado
num extremo, que é o que torna limiar frágil à primeira variação. Um eval
(`tests/eval/`) protege essa propriedade e roda no CI: se a faixa fechar, os
limiares perderam a justificativa.

`LIMIAR_PARCIAL = 0,62` tem outra lógica: não separa certo de errado, define o que
vale o tempo de um analista. Fica **abaixo** do maior negativo de propósito, porque
nessa faixa moram os casos genuinamente ambíguos — nome consultado que é
subconjunto do da lista, ou nome reordenado.

## Camadas

```
domain/
  matching.py    Normalização, tokenização e comparação de nomes
  triagem.py     Classificação, decisão por tipo de lista e trilha
application/
  ports.py       RepositorioListas (síncrono) e RepositorioTriagens (async)
  use_cases/     Orquestração: lê a lista, delega a decisão, persiste
infrastructure/
  listas.py      Carregamento de CSV no boot, ou entradas em memória
  repositories/  Triagens em memória (Postgres quando houver retenção real)
api/             FastAPI: contrato HTTP separado das entidades
```

## Falha alto, e isso é a decisão de disponibilidade central

O serviço **se recusa a subir** em três situações: diretório de listas inexistente,
nenhuma entrada carregada, e linha com tipo inválido no CSV.

Todas pelo mesmo motivo: um serviço de conformidade com lista incompleta aprova
quem deveria barrar — e reporta "nenhuma correspondência" ao fazer isso.
Degradação silenciosa na direção mais perigosa possível. Por isso `/ready` também
verifica `entradas_carregadas > 0`: um pod sem lista sai do load balancer.

## API

| Método | Rota | Descrição |
|---|---|---|
| `POST` | `/v1/triagens` | Tria um cliente; devolve decisão, correspondências e justificativas |
| `GET` | `/v1/triagens/{id}` | Consulta uma triagem (registro de diligência) |
| `GET` | `/v1/triagens` | Lista paginada |
| `GET` | `/health` | Liveness |
| `GET` | `/ready` | Readiness — inclui a contagem de entradas carregadas |

O CPF sai **sempre mascarado** na resposta. O `X-Request-ID` recebido é
reaproveitado, não regenerado: é o que permite seguir uma análise de crédito que
consultou este serviço nos logs dos dois.

### Exemplo

```bash
curl -X POST http://localhost:8100/v1/triagens \
  -H 'Content-Type: application/json' \
  -d '{"nome": "Carlos Eduardo Lima", "cpf": "529.982.247-25"}'
```

## Dependências: sete, contra vinte e uma do outro serviço

Sem NumPy, pandas, OpenCV, ONNX, LangChain nem cliente de banco. A imagem sai em
torno de 200MB contra 1,18GB — e isso importa porque este serviço é **dependência**
do outro: quando ele cai, toda análise de crédito vai para revisão humana. Um
serviço nessa posição precisa subir rápido, escalar barato e ter pouca superfície
de CVE.

## Testes

```bash
.venv/Scripts/python -m pytest              # 58 testes
.venv/Scripts/python -m pytest -m eval      # medição do casamento de nomes
```

Os evals deste serviço **rodam no CI**, diferente dos do `credit-analysis`: não
dependem de baixar modelo nem de binário externo, custam segundos, e protegem a
propriedade que sustenta os limiares de decisão.
