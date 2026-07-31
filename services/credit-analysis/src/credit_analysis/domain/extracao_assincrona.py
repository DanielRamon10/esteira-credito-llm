"""Contrato das mensagens do fluxo assincrono de extracao.

## Por que estas classes ficam em `domain` e nao em `infrastructure`

Elas descrevem **o que** trafega, nao **como**. `PedidoDeExtracao` e o mesmo objeto quer a fila
seja SQS, ElasticMQ ou uma lista em memoria; o adapter traduz para JSON e de volta.

Colocá-las em `infrastructure` faria o caso de uso importar de la para saber o que publicar, e
a regra de dependencia do projeto e que as setas apontam para dentro.

## Por que o pedido nao carrega o conteudo do documento

Ele carrega uma `Referencia`. Um holerite escaneado tem alguns megabytes, e o limite de mensagem
do SQS e 256KB — mas o limite nao e a razao principal.

A razao e que **mensagem e copia**. Conteudo dentro da mensagem existiria em dois lugares (fila
e armazenamento) e os dois poderiam divergir: uma nova tentativa reprocessaria os bytes da
mensagem antiga, enquanto a auditoria leria os do armazenamento. Com referencia, ha uma unica
fonte, e ela e imutavel por versao.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from credit_analysis.domain.armazenamento import Referencia
from credit_analysis.domain.enums import TipoDocumento

# Versao do formato da mensagem.
#
# Existe porque fila tem **estado**: durante um deploy, mensagens publicadas pela versao antiga
# sao consumidas pela nova. Sem o campo, um campo renomeado faria o consumidor novo levantar
# `KeyError` numa mensagem que ele nao tem como reconhecer — e o erro apontaria para o consumidor
# em vez de para a incompatibilidade.
#
# Com ele, o consumidor recusa explicitamente o que nao entende, e a mensagem vai para descarte
# com motivo legivel em vez de virar laco de tentativa.
VERSAO_DO_CONTRATO = 1


class ContratoIncompativel(Exception):
    """Mensagem de uma versao que este consumidor nao entende.

    Erro **permanente**, nao transitorio: tentar de novo com o mesmo consumidor da o mesmo
    resultado. Vai direto para descarte.
    """


@dataclass(frozen=True, slots=True)
class PedidoDeExtracao:
    """O que a API publica quando recebe um documento."""

    analise_id: UUID
    documento_id: UUID
    referencia: Referencia
    tipo: TipoDocumento
    nome_arquivo: str

    # Propagado da requisicao HTTP que originou o pedido.
    #
    # Sem isto, a trilha de uma analise se parte no momento em que o trabalho vira assincrono: o
    # log da API tem o `request_id`, o log da extracao tem outro, e cruzar os dois exige
    # arqueologia de timestamp. Foi o mesmo raciocinio que levou o cliente do KYC a propagar o
    # cabecalho de correlacao.
    request_id: str = ""

    def para_json(self) -> str:
        return json.dumps(
            {
                "versao": VERSAO_DO_CONTRATO,
                "analise_id": str(self.analise_id),
                "documento_id": str(self.documento_id),
                "chave": self.referencia.chave,
                "versao_objeto": self.referencia.versao,
                "tipo": self.tipo.value,
                "nome_arquivo": self.nome_arquivo,
                "request_id": self.request_id,
            },
            ensure_ascii=False,
        )

    @classmethod
    def de_json(cls, bruto: str) -> PedidoDeExtracao:
        dados: dict[str, Any] = json.loads(bruto)

        versao = dados.get("versao")
        if versao != VERSAO_DO_CONTRATO:
            raise ContratoIncompativel(
                f"mensagem na versao {versao!r}, este consumidor entende {VERSAO_DO_CONTRATO}"
            )

        return cls(
            analise_id=UUID(dados["analise_id"]),
            documento_id=UUID(dados["documento_id"]),
            referencia=Referencia(chave=dados["chave"], versao=dados["versao_objeto"]),
            tipo=TipoDocumento(dados["tipo"]),
            nome_arquivo=dados["nome_arquivo"],
            request_id=dados.get("request_id", ""),
        )


@dataclass(frozen=True, slots=True)
class Entrega:
    """Uma mensagem retirada da fila, com o que a fila precisa para confirma-la.

    `recibo` e opaco de proposito: no SQS e o `ReceiptHandle`, na fila em memoria e um indice.
    O caso de uso nao interpreta — ele devolve para `confirmar` ou `devolver`.

    `tentativas` vem da fila, nao de um contador nosso. No SQS e o
    `ApproximateReceiveCount`, e usa-lo em vez de contar do nosso lado importa: a contagem da
    fila sobrevive ao reinicio do trabalhador, e a nossa nao.
    """

    pedido: PedidoDeExtracao
    recibo: str
    tentativas: int = 1
