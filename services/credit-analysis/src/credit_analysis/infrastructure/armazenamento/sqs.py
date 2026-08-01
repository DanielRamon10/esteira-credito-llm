"""Fila de trabalho em SQS (ou ElasticMQ, que fala o mesmo protocolo).

## O teto de tentativas muda de lugar aqui, e isso e a diferenca que importa

O adapter em memoria conta tentativas e move para a fila de descarte quando estoura o teto —
porque nao ha fila de verdade para fazer isso.

No SQS o teto e **configuracao da fila**, na `RedrivePolicy` (`maxReceiveCount` mais
`deadLetterTargetArn`), e nao codigo. Reimplementa-lo aqui seria pior que redundante: as duas
contagens divergiriam, e a do SQS sobrevive ao reinicio do trabalhador enquanto a nossa nao —
um documento que ja consumiu quatro das cinco tentativas voltaria a ter cinco depois de um
deploy.

Por isso `devolver` aqui apenas **torna a mensagem visivel de novo** (`visibility timeout = 0`), e
deixa o SQS decidir entre reentrega e descarte. A consequencia pratica: o teto vive no Terraform,
e mudar de 3 para 5 tentativas e um `apply`, nao um deploy.

## `ApproximateReceiveCount` e aproximado, e ainda assim e o numero certo

O nome nao esconde nada: em cenarios raros o SQS pode entregar a mesma mensagem duas vezes e
contar uma. Continua sendo melhor que contar do nosso lado, porque a contagem dele atravessa
reinicio de processo. E o consumidor nao decide nada com base nela — quem decide o descarte e a
fila. Ela vai para o log, onde "tentativa 4 de 5" e informacao util durante incidente.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import boto3
import structlog
from botocore.config import Config

from credit_analysis.domain.extracao_assincrona import (
    ContratoIncompativel,
    Entrega,
    PedidoDeExtracao,
)

if TYPE_CHECKING:
    from mypy_boto3_sqs.client import SQSClient
    from mypy_boto3_sqs.type_defs import MessageTypeDef

logger = structlog.get_logger(__name__)

# Teto do long polling do SQS. Acima disso a API recusa.
ESPERA_MAXIMA_SEGUNDOS = 20

# Quanto tempo a mensagem fica invisivel depois de consumida.
#
# Precisa cobrir o pior caso da extracao: OCR com escalonamento pode chamar modelo de visao, e o
# medido neste projeto chega a 148s. Com um valor curto, o SQS reentregaria a mensagem **enquanto
# o trabalhador ainda esta processando** — dois trabalhadores no mesmo documento, e a idempotencia
# salvaria o resultado mas o custo de OCR seria pago duas vezes.
#
# 300s da folga sobre os 148s. O preco de errar para cima e uma mensagem presa por 5 minutos
# quando o trabalhador morre; para baixo, e trabalho duplicado em regime normal.
VISIBILIDADE_SEGUNDOS = 300


class FilaSQS:
    """Adapter do port `FilaDeTrabalho` sobre SQS ou ElasticMQ."""

    __slots__ = ("_cliente", "_url")

    def __init__(
        self,
        url_da_fila: str,
        *,
        regiao: str = "sa-east-1",
        endpoint_url: str | None = None,
        cliente: Any = None,
    ) -> None:
        self._url = url_da_fila
        self._cliente: SQSClient = cliente or boto3.client(
            "sqs",
            region_name=regiao,
            endpoint_url=endpoint_url,
            config=Config(
                retries={"max_attempts": 3, "mode": "adaptive"},
                connect_timeout=5,
                # Maior que o long polling: com `read_timeout` menor que `WaitTimeSeconds`, cada
                # espera vazia viraria um timeout de socket em vez de uma resposta vazia — e o log
                # se enche de erro de rede num sistema que esta apenas ocioso.
                read_timeout=ESPERA_MAXIMA_SEGUNDOS + 10,
            ),
        )

    async def publicar(self, pedido: PedidoDeExtracao) -> None:
        def _enviar() -> None:
            self._cliente.send_message(QueueUrl=self._url, MessageBody=pedido.para_json())

        await asyncio.to_thread(_enviar)

    async def consumir(
        self, quantidade: int = 1, espera_segundos: int = ESPERA_MAXIMA_SEGUNDOS
    ) -> list[Entrega]:
        def _receber() -> list[MessageTypeDef]:
            resposta = self._cliente.receive_message(
                QueueUrl=self._url,
                # Teto de 10 por chamada, imposto pela API.
                MaxNumberOfMessages=min(quantidade, 10),
                WaitTimeSeconds=min(espera_segundos, ESPERA_MAXIMA_SEGUNDOS),
                VisibilityTimeout=VISIBILIDADE_SEGUNDOS,
                # Sem pedir explicitamente, o SQS nao devolve a contagem — e o log ficaria sem
                # "tentativa N", que e a informacao que distingue "fila lenta" de "documento
                # preso em laco".
                MessageSystemAttributeNames=["ApproximateReceiveCount"],
            )
            return list(resposta.get("Messages", []))

        mensagens = await asyncio.to_thread(_receber)

        entregas: list[Entrega] = []
        for mensagem in mensagens:
            recibo = str(mensagem["ReceiptHandle"])
            try:
                pedido = PedidoDeExtracao.de_json(str(mensagem["Body"]))
            except (ContratoIncompativel, KeyError, ValueError) as exc:
                # Mensagem que nem parseia nao pode virar `Entrega`: nao ha `pedido` para o
                # trabalhador logar nem para marcar o documento. Ela e apagada aqui mesmo, com o
                # corpo no log.
                #
                # Apagar e nao devolver porque o erro e **permanente** por definicao: o mesmo
                # bytes produz o mesmo erro de parse. Devolver a mandaria para a DLQ depois de N
                # tentativas inuteis, atrasando a unica acao possivel — alguem olhar o log.
                logger.error(
                    "fila.mensagem_ilegivel",
                    erro=type(exc).__name__,
                    mensagem=str(exc),
                    corpo=str(mensagem.get("Body", ""))[:500],
                )
                await self._apagar(recibo)
                continue

            entregas.append(
                Entrega(
                    pedido=pedido,
                    recibo=recibo,
                    tentativas=int(
                        mensagem.get("Attributes", {}).get("ApproximateReceiveCount", 1)
                    ),
                )
            )
        return entregas

    async def confirmar(self, entrega: Entrega) -> None:
        await self._apagar(entrega.recibo)

    async def devolver(self, entrega: Entrega, motivo: str) -> None:
        """Torna a mensagem visivel de novo. **Quem decide o descarte e a fila.**

        `VisibilityTimeout=0` em vez de reenviar a mensagem: reenviar criaria uma mensagem nova,
        com `ApproximateReceiveCount` reiniciado — e a `RedrivePolicy` nunca alcancaria o teto. O
        documento ficaria em laco para sempre, que e exatamente o que a DLQ existe para impedir.
        """

        def _liberar() -> None:
            self._cliente.change_message_visibility(
                QueueUrl=self._url, ReceiptHandle=entrega.recibo, VisibilityTimeout=0
            )

        await asyncio.to_thread(_liberar)
        logger.info(
            "fila.devolvida",
            documento_id=str(entrega.pedido.documento_id),
            tentativas=entrega.tentativas,
            motivo=motivo,
        )

    async def _apagar(self, recibo: str) -> None:
        def _executar() -> None:
            self._cliente.delete_message(QueueUrl=self._url, ReceiptHandle=recibo)

        await asyncio.to_thread(_executar)

    @property
    def identificacao(self) -> str:
        return f"sqs:{self._url}"
