"""Medicao da qualidade do casamento de nomes.

Marcado como `eval` e fora da suite padrao pelo mesmo motivo dos evals do outro
servico: **isto e medicao, nao teste binario**. O resultado e "separacao de 0,23
entre os grupos", e transformar isso em passou/falhou exige escolher um limiar que,
no lugar errado, quebra o pipeline por variacao normal.

O que este arquivo protege e a **propriedade estrutural** que sustenta os limiares:
existe uma faixa vazia entre o pior positivo e o melhor negativo. Se ela fechar,
os limiares de `triagem.py` perderam a justificativa e precisam ser reavaliados —
e essa mensagem e mais util que um assert falhando em silencio.

    ruff: nao ha `S` aqui porque nao ha segredo; nao ha rede porque nao ha modelo.
"""

from __future__ import annotations

import pytest

from kyc_compliance.domain.matching import comparar
from kyc_compliance.domain.triagem import LIMIAR_FORTE, LIMIAR_PARCIAL

pytestmark = pytest.mark.eval

# Mesma pessoa, escrita de formas diferentes.
MESMA_PESSOA = [
    ("Jose da Silva Junior", "JOSE DA SILVA JR."),
    ("Jose Antonio Pereira", "JOSE ANTONIO PEREIRA"),
    ("Maria Fernanda Souza", "MARIA FERNANDA SOUZA"),
    ("Carlos Eduardo Lima", "CARLOS E. LIMA"),
    ("Ana Paula Rodrigues", "ANA PAULA RODRIGES"),
    ("Roberto Carlos de Almeida", "ROBERTO CARLOS ALMEIDA"),
    ("Fernanda Silva Neto", "FERNANDA SILVA NETO"),
    ("Joao Pedro Nascimento", "JOAO PEDRO DO NASCIMENTO"),
]

# Pessoas diferentes que um algoritmo ingenuo confundiria.
PESSOAS_DIFERENTES = [
    ("Maria Silva", "Mario Silva"),
    ("Ana Costa", "Ana Souza"),
    ("Carlos Lima", "Carla Lima"),
    ("Pedro Henrique Alves", "Paulo Henrique Alves"),
    ("Lucas Martins", "Lucas Martinez"),
    ("Rafael Gomes", "Gomes Rafael Sobrinho"),
]


def test_ha_faixa_vazia_entre_os_dois_grupos() -> None:
    """A propriedade que justifica os limiares.

    Medido na construcao: positivos a partir de 0,934, negativos ate 0,703 — uma
    faixa vazia de 0,23. `LIMIAR_FORTE = 0,85` fica no meio dela, com margem para
    os dois lados, e nao encostado num extremo (que e o que torna limiar fragil).
    """
    positivos = [comparar(a, b)[0] for a, b in MESMA_PESSOA]
    negativos = [comparar(a, b)[0] for a, b in PESSOAS_DIFERENTES]

    pior_positivo = min(positivos)
    melhor_negativo = max(negativos)

    assert pior_positivo > melhor_negativo, (
        f"a faixa vazia fechou: pior positivo {pior_positivo:.3f} <= "
        f"melhor negativo {melhor_negativo:.3f}. Os limiares de triagem.py "
        f"perderam a justificativa e precisam ser reavaliados."
    )

    assert melhor_negativo < LIMIAR_FORTE < pior_positivo, (
        f"LIMIAR_FORTE={LIMIAR_FORTE} saiu da faixa vazia "
        f"({melhor_negativo:.3f} a {pior_positivo:.3f})"
    )


def test_nenhum_negativo_alcanca_o_limiar_forte() -> None:
    """Falso positivo em nivel forte e o erro mais caro deste dominio.

    Nao porque a consequencia seja pior que a de um falso negativo — e menos grave —
    mas porque em nivel forte a decisao e automatica: sancao com casamento forte
    reprova sem passar por humano.
    """
    for a, b in PESSOAS_DIFERENTES:
        score, _, _ = comparar(a, b)
        assert score < LIMIAR_FORTE, f"{a!r} vs {b!r} atingiu {score:.3f}"


def test_sensibilidade_na_faixa_de_revisao() -> None:
    """Todo positivo tem de ao menos chegar a revisao humana.

    Este e o assert que protege contra o erro regulatoriamente relevante: deixar
    passar quem esta na lista.
    """
    for a, b in MESMA_PESSOA:
        score, _, _ = comparar(a, b)
        assert score >= LIMIAR_PARCIAL, f"{a!r} vs {b!r} ficou invisivel ({score:.3f})"
