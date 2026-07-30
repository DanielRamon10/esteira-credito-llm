"""Medicao da busca sobre o corpus REAL.

Marcado como `eval` e fora da suite padrao: e medicao, nao teste binario. Mas roda no
CI porque nao depende de nada externo — BM25 e stdlib, sem modelo para baixar.

O que se mede: BM25 sozinho basta para esta base? A resposta justifica nao ter
embedding neste servico, e se ela mudar (base cresce, perguntas ficam mais
parafraseadas) a decisao precisa ser revista.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from customer_support.infrastructure.conhecimento import ConhecimentoEmArquivos

pytestmark = pytest.mark.eval

# Perguntas como um cliente escreveria, com o artigo que deveria vir em primeiro.
PERGUNTAS: list[tuple[str, str]] = [
    ("Quais documentos preciso enviar para comprovar minha renda?", "comprovacao-renda"),
    ("Sou autonomo, como comprovo renda?", "comprovacao-renda"),
    ("Como faco para trazer meu emprestimo de outro banco?", "portabilidade"),
    ("Portabilidade tem tarifa?", "portabilidade"),
    ("O que significa CET?", "cet"),
    ("Como comparar propostas de bancos diferentes?", "cet"),
    ("Posso pagar parcelas antes do vencimento?", "antecipacao"),
    ("Tenho desconto se quitar o contrato?", "antecipacao"),
    ("Quanto tempo demora a analise?", "prazo-analise"),
    ("Por que uma proposta e negada?", "motivos-negativa"),
    ("Posso pedir revisao de uma negativa?", "motivos-negativa"),
    ("Como funciona o desconto em folha?", "consignado"),
]


@pytest.fixture(scope="module")
def base() -> ConhecimentoEmArquivos:
    return ConhecimentoEmArquivos(Path("conhecimento"))


def test_acerto_no_top1_e_top3(base: ConhecimentoEmArquivos) -> None:
    """Reporta os dois numeros; falha apenas em colapso.

    Limiar frouxo de proposito: um limiar apertado quebraria o pipeline por variacao
    normal de redacao de artigo. O que este teste pega e o cenario em que a busca
    parou de funcionar.
    """
    top1 = top3 = 0

    for pergunta, esperado in PERGUNTAS:
        resultados = base.buscar(pergunta, k=3, apenas_publicos=True)
        ids = [r.artigo.id for r in resultados]

        top1 += bool(ids and ids[0] == esperado)
        top3 += esperado in ids

        if not ids or ids[0] != esperado:
            print(f"  top1 errou: {pergunta!r} -> {ids} (esperado {esperado})")

    total = len(PERGUNTAS)
    print(f"\nBM25 sobre {base.publicos} artigos publicos:")
    print(f"  top-1: {top1}/{total} ({top1 / total:.0%})")
    print(f"  top-3: {top3}/{total} ({top3 / total:.0%})")

    assert top3 / total >= 0.75, (
        f"top-3 em {top3 / total:.0%}: a busca lexical deixou de servir para esta base. "
        f"Revisar a decisao de nao usar embedding (ver domain/conhecimento.py)."
    )


def test_nenhum_artigo_interno_aparece(base: ConhecimentoEmArquivos) -> None:
    """Sobre o corpus real, e nao um inventado.

    Um teste de vazamento com corpus de teste nao prova nada sobre o que e servido.
    """
    for pergunta, _ in [*PERGUNTAS, ("qual o score minimo para aprovacao?", ""), ("alcada", "")]:
        for resultado in base.buscar(pergunta, k=5, apenas_publicos=True):
            assert resultado.artigo.publico, f"{resultado.artigo.id} vazou em {pergunta!r}"


def test_o_guard_bloqueia_o_artigo_interno_real(base: ConhecimentoEmArquivos) -> None:
    """Fecha o circuito: a segunda defesa contra o conteudo real da primeira."""
    from customer_support.domain.divulgacao import inspecionar

    internos = [a for a in base.todos() if not a.publico]
    assert internos, "o corpus precisa ter artigo interno para este teste valer"

    for artigo in internos:
        veredito = inspecionar(artigo.texto)
        assert veredito.bloqueada, f"{artigo.id} passaria pelo guard de divulgacao"
