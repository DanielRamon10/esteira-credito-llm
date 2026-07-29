---
id: POL-003
titulo: Tratamento de Restrições Cadastrais
versao: "4.1"
vigencia_inicio: 2025-03-01
area: Risco de Crédito Pessoa Física
produtos: [cdc, credito_pessoal, consignado, cartao]
natureza: sintetico
---

# POL-003 — Tratamento de Restrições Cadastrais

## 1. Fontes consultadas

A consulta cadastral é obrigatória em toda operação e abrange:

- bureaus de crédito (Serasa Experian, SPC Brasil, Boa Vista);
- Cadastro de Emitentes de Cheques sem Fundos (CCF) do Banco Central;
- base interna de operações em atraso da instituição.

A consulta tem validade de 30 dias. Parecer emitido com consulta vencida é
considerado sem consulta.

## 2. Classificação das restrições

| Tipo | Descrição | Efeito |
|---|---|---|
| Impeditiva | Protesto ativo, execução judicial, CCF | Negativa automática |
| Grave | Atraso acima de 90 dias em operação ativa | Negativa automática |
| Moderada | Atraso entre 30 e 90 dias | Análise manual obrigatória |
| Leve | Consulta recente por outras instituições (>5 em 30 dias) | Fator de risco no score |
| Baixada | Restrição regularizada há mais de 12 meses | Sem efeito |

Restrição classificada como Impeditiva ou Grave resulta em **negativa
automática da operação**, independentemente do score obtido, do valor
solicitado ou do comprometimento de renda apurado. Este é um veto duro e não
é ponderável com outros fatores.

## 3. Restrição regularizada

Restrição baixada há menos de 12 meses mantém efeito de fator de risco
moderado no score, com peso decrescente:

| Tempo desde a baixa | Tratamento |
|---|---|
| até 3 meses | equivalente a restrição Moderada |
| acima de 3 até 12 meses | fator de risco no score |
| acima de 12 meses | sem efeito |

## 4. Divergência entre fontes

Quando uma fonte acusar restrição e outra não, prevalece a fonte mais
restritiva e o caso é encaminhado para análise manual. Não é permitido
selecionar a fonte mais favorável ao cliente.

## 5. Direito de revisão

Conforme o art. 20 da Lei 13.709/2018 (LGPD), o titular tem direito a solicitar
revisão de decisão tomada exclusivamente com base em tratamento automatizado.

Toda negativa fundamentada em restrição cadastral deve:

1. registrar no parecer a fonte e o tipo da restrição;
2. informar ao cliente o canal para solicitar revisão humana;
3. preservar o registro de auditoria por no mínimo 5 anos.

Não é permitido comunicar ao cliente apenas o resultado ("negado") sem a
fundamentação correspondente.
