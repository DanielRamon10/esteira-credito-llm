---
id: limiares-score
titulo: "Limiares de score, pesos e alçadas (uso interno)"
visibilidade: interna
atualizado_em: "2026-07-01"
---
> Artigo **interno**. Não deve ser servido a cliente em nenhuma circunstância. Ele
> existe nesta base de propósito: é o material realista contra o qual o guard de
> divulgação (`domain/divulgacao.py`) é testado. Um teste de vazamento com corpus
> inventado não prova nada sobre o corpus real.

Faixas de decisão automática:

- Score igual ou acima de 700 pontos: aprovação direta.
- Entre 500 e 699: análise manual por especialista.
- Abaixo de 350: negativa automática.

Pesos do modelo de score: comprometimento de renda 40%, divergência de renda 20%,
histórico bancário 15%, restrição cadastral 15%, perfil demográfico 10%.

Conforme a POL-001, comprometimento de renda acima de 50% é vedado e nenhuma alçada
ordinária autoriza aprovação acima desse teto. A alçada do gerente regional aprova
até R$ 150.000 com score igual ou superior a 600.
