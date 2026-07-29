---
id: POL-006
titulo: Alçadas de Aprovação e Governança da Decisão Automatizada
versao: "1.9"
vigencia_inicio: 2025-04-01
area: Governança de Crédito
produtos: [cdc, credito_pessoal, consignado, cartao]
natureza: sintetico
---

# POL-006 — Alçadas de Aprovação e Governança da Decisão Automatizada

## 1. Alçadas

| Alçada | Valor máximo | Condições |
|---|---|---|
| Esteira automatizada | R$ 50.000 | Score ≥ 700, comprometimento ≤ 30%, sem restrição |
| Gerente de relacionamento | R$ 150.000 | Score ≥ 600, comprometimento ≤ 40% |
| Comitê regional | R$ 500.000 | Score ≥ 500 |
| Comitê de crédito | sem teto | Deliberação colegiada registrada em ata |

Nenhuma alçada, incluindo o Comitê de Crédito, pode aprovar operação com
restrição cadastral Impeditiva ativa (POL-003, seção 2).

## 2. Encaminhamento obrigatório para análise manual

A esteira automatizada **não decide** e encaminha para análise humana quando:

- o score ficar na faixa Média (500 a 699);
- houver divergência de renda superior a 30% (POL-002, seção 5);
- houver restrição cadastral Moderada (POL-003, seção 2);
- a confiança da extração documental ficar abaixo de 85% (POL-002, seção 3.2);
- houver saldo negativo recorrente em renda variável (POL-005, seção 5);
- as fontes de consulta cadastral divergirem entre si (POL-003, seção 4).

A ausência de decisão automática não é falha da esteira: encaminhar um caso
limítrofe para revisão humana é o comportamento correto.

## 3. Requisitos de explicabilidade

Toda decisão produzida pela esteira automatizada deve registrar:

1. o resultado (aprovado, aprovado com ressalvas, negado, análise manual);
2. o score obtido e a contribuição de cada fator;
3. as políticas aplicadas, identificadas por código e versão;
4. os dados utilizados e sua procedência (declarado, extraído, calculado);
5. o identificador de correlação da requisição.

Parecer sem justificativa rastreável não pode ser comunicado ao cliente.

## 4. Vedações ao uso de fatores

É vedado o uso, como fator de decisão, de:

- origem racial ou étnica, convicção religiosa, opinião política;
- filiação a sindicato ou organização religiosa, filosófica ou política;
- dado referente à saúde ou à vida sexual;
- dado genético ou biométrico.

Fatores demográficos admitidos (idade, região) têm peso limitado a 10% do score
agregado e não podem, isoladamente, determinar o resultado de uma análise.

## 5. Retenção e auditoria

O registro completo da decisão — entradas, políticas aplicadas, score, parecer
e identificador de correlação — deve ser retido por no mínimo 5 anos e ser
recuperável a partir do CPF do solicitante ou do identificador da análise.

## 6. Revisão periódica do modelo

Os pesos dos fatores de score são revistos semestralmente pelo comitê de
modelos. Toda alteração de peso ou de limiar exige:

- nova versão da política correspondente;
- registro da data de vigência;
- backtesting sobre a safra dos 12 meses anteriores.
