"""Rotas de direitos do titular (LGPD).

## Por que um router proprio, e nao um `DELETE /v1/analises/{id}`

O verbo HTTP quase encaixa, e o recurso nao. Um pedido de exclusao do titular:

- **nao tem um id** — ele chega por CPF, e alcanca todas as analises daquela pessoa. Um `DELETE` por
  id obrigaria quem atende a descobrir os ids primeiro, o que significa listar dado pessoal para
  poder apagar dado pessoal;
- **nao e idempotente do jeito que `DELETE` promete** — a segunda chamada nao encontra nada e isso
  nao e erro, e o recibo da segunda difere do da primeira;
- **produz um documento** — o recibo do art. 19, que o titular tem direito de receber. `DELETE` que
  devolve corpo com conteudo obrigatorio nao e o desenho que a semantica sugere.

`POST /v1/privacidade/apagamentos` cria um registro de atendimento, e e isso que esta acontecendo.

## O CPF vai no corpo, e isso nao e detalhe

`POST /v1/privacidade/apagamentos/{cpf}` seria mais curto e poria o CPF em: log de acesso do
ingress, historico do navegador, header `Referer`, span de trace, e — no pior caso — label de
metrica por rota. Cinco lugares sem controle de acesso a dado pessoal, num pedido cujo objeto e
**remover** aquele dado.

O projeto ja toma essa decisao para metricas (ver `Cardinalidade e LGPD` no README); aqui ela vale
com mais forca, porque a rota existe justamente para atender quem nao quer o dado por ai.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, status

from credit_analysis.api.deps import CicloDeVidaDep
from credit_analysis.api.schemas import ErroResponse, PedidoDeApagamento, ReciboResponse
from credit_analysis.api.seguranca import ANALISES_ESCREVER, Escopo
from credit_analysis.application.use_cases.ciclo_de_vida import ApagarDadoPessoal

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/privacidade", tags=["Privacidade"])


@router.post(
    "/apagamentos",
    response_model=ReciboResponse,
    status_code=status.HTTP_200_OK,
    summary="Atender pedido de exclusao do titular (LGPD art. 18)",
    # `analises:escrever` e nao um escopo proprio de privacidade.
    #
    # A tentacao seria criar `privacidade:apagar`, e ela erra por um motivo concreto: escopo novo
    # significa token novo, e o time de atendimento — que e quem opera isto — passaria a ter uma
    # credencial extra para guardar. Apagar analise e a escrita mais destrutiva que existe sobre
    # analise, e quem pode escrever ja pode reescrever.
    #
    # O que **nao** serve e `analises:ler`: leitura nao deveria destruir.
    dependencies=[Depends(Escopo(ANALISES_ESCREVER))],
    responses={
        # 503 e nao 501: a capacidade depende de configuracao (`CREDIT_POSTGRES_DSN`), o que a
        # coloca na mesma classe do OCR ausente. O raciocinio esta em `obter_ciclo_de_vida`.
        503: {
            "model": ErroResponse,
            "description": "Ambiente sem persistencia duravel; nada a apagar a pedido",
        }
    },
)
async def apagar_dado_pessoal(
    pedido: PedidoDeApagamento,
    ciclo: CicloDeVidaDep,
) -> ReciboResponse:
    """Apaga a identificacao do titular e conserva o registro das decisoes.

    ## Sempre 200, inclusive quando nao havia nada

    Um `404` para CPF sem analise responderia a pergunta "esta pessoa tem cadastro aqui?" a quem
    tiver o escopo e uma lista de CPFs — um oraculo de existencia construido pela rota que existe
    para proteger a pessoa.

    Com `200` e `analises_afetadas: []`, as duas situacoes tem a mesma forma de resposta, e o
    atendimento fica registrado do mesmo jeito. O titular que nao tinha cadastro recebe a informacao
    correta: nada foi encontrado, nada foi apagado.

    ## O recibo diz o que conservamos, e por que

    `decisoes_conservadas` nao e uma ressalva escondida no rodape: e o campo que informa ao titular
    que o registro da decisao permanece sob obrigacao legal (art. 16 §I), sem identificacao. Omitir
    isso seria dar um recibo de exclusao total sobre uma exclusao parcial.
    """
    recibo = await ApagarDadoPessoal(ciclo).executar(pedido.cpf_valido)
    return ReciboResponse.de_dominio(recibo)
