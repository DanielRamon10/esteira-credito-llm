"""Endpoint do agente de credito (Camada 4).

Sincrono de proposito, mesmo custando ~80s por atendimento com uma ferramenta e
modelo local em CPU (medido; ver o cabecalho de `agente/grafo.py`). A alternativa
correta para produto — 202 mais polling, ou fila com o grafo retomando de
checkpoint — esconderia o custo atras de um spinner numa fase em que o objetivo e
justamente medi-lo. A Camada 5 instrumenta; ai a decisao de tornar assincrono
passa a ser tomada com numero, nao com impressao.

O contrato devolve a trilha junto da resposta. Nao e detalhe de depuracao: sem
`motivo_parada`, uma resposta cortada pelo teto de ferramentas e indistinguivel
de uma resposta completa para quem consome a API.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, status

from credit_analysis.api.deps import AgenteDep
from credit_analysis.api.schemas import PerguntaAgenteRequest, TrilhaAgenteResponse

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/agente", tags=["agente"])


@router.post(
    "/consultar",
    response_model=TrilhaAgenteResponse,
    status_code=status.HTTP_200_OK,
    summary="Pergunta ao agente, que decide quais ferramentas usar",
    responses={
        503: {"description": "Nenhum modelo com suporte a ferramentas disponivel"},
    },
)
async def consultar(pedido: PerguntaAgenteRequest, agente: AgenteDep) -> TrilhaAgenteResponse:
    """Responde a pergunta consultando politica, caso e simulacao.

    `analise_id` e opcional e vem **daqui**, do corpo da requisicao — nunca do
    texto que o modelo gera. E o que impede o agente de trocar de caso no meio
    da conversa, por alucinacao ou por instrucao plantada num documento.
    """
    trilha = await agente.atender(pedido.pergunta, analise_id=pedido.analise_id)
    return TrilhaAgenteResponse.de_dominio(trilha)
