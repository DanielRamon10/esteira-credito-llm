"""Fixtures compartilhadas.

As entradas de teste sao definidas aqui e nao lidas de `dados/listas`: teste que
depende de arquivo de dado quebra quando alguem edita o arquivo por outro motivo.
O carregamento do CSV tem teste proprio, com arquivo temporario.
"""

from __future__ import annotations

import pytest

from kyc_compliance.config import Ambiente, Settings
from kyc_compliance.domain.triagem import EntradaRestritiva, TipoLista

# CPF valido que NAO pertence a ninguem nas listas de teste. Existe como constante
# porque reusar o CPF de uma entrada por descuido faz o teste passar pelo motivo
# errado — aconteceu de verdade durante a construcao deste servico.
CPF_LIMPO = "390.533.447-05"

CPF_DA_PEP = "529.982.247-25"
CPF_DO_SANCIONADO = "111.444.777-35"


@pytest.fixture
def entradas() -> list[EntradaRestritiva]:
    return [
        EntradaRestritiva(
            nome="JOSE DA SILVA JR.",
            tipo=TipoLista.PEP,
            origem="teste-pep",
            cargo="Prefeito municipal",
        ),
        # Mesma pessoa em duas listas: e o caso que um indice por nome colapsaria.
        EntradaRestritiva(
            nome="JOSE DA SILVA JR.",
            tipo=TipoLista.SANCAO,
            origem="teste-sancoes",
        ),
        EntradaRestritiva(
            nome="MARIA FERNANDA SOUZA",
            tipo=TipoLista.PEP,
            origem="teste-pep",
            cpf=CPF_DA_PEP,
        ),
        EntradaRestritiva(
            nome="CARLOS E. LIMA",
            tipo=TipoLista.PEP,
            origem="teste-pep",
        ),
        EntradaRestritiva(
            nome="MARCOS VINICIUS TEIXEIRA",
            tipo=TipoLista.SANCAO,
            origem="teste-sancoes",
            cpf=CPF_DO_SANCIONADO,
        ),
        EntradaRestritiva(
            nome="JOSE ANTONIO PEREIRA",
            tipo=TipoLista.SANCAO,
            origem="teste-sancoes",
        ),
        EntradaRestritiva(
            nome="PATRICIA GOMES DE OLIVEIRA",
            tipo=TipoLista.MIDIA_NEGATIVA,
            origem="teste-midia",
        ),
    ]


@pytest.fixture
def settings_teste() -> Settings:
    return Settings(ambiente=Ambiente.LOCAL, nivel_log="WARNING", log_json=False)
