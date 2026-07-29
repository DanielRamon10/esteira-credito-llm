"""Infraestrutura tecnica compartilhada entre os servicos do monorepo.

Regra de dependencia: **esta biblioteca nao conhece Prometheus nem
OpenTelemetry**. Ela expoe ganchos de observacao (`registrar_observador`) e cada
servico decide o que medir — do contrario, todo consumidor futuro ficaria preso a
mesma stack de observabilidade.

Regra de escopo: **dominio nao mora aqui**. Nada de CPF, Dinheiro ou entidade de
negocio; cada servico e um bounded context e evolui por pressao propria. O preco e
alguma duplicacao consciente, anotada nos dois lados.
"""

__all__ = ["bm25", "llm", "logging", "seguranca"]
