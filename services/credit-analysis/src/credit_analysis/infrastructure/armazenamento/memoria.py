"""Armazenamento e fila em memoria.

## Por que estes adapters existem, e o que eles NAO sao

Eles nao sao "o fake para os testes". Sao o que permite o fluxo assincrono inteiro rodar sem
conta em nuvem — que e restricao declarada do projeto — e por isso implementam o comportamento
que **importa**, nao o mais simples:

- versionamento de verdade, porque a deduplicacao depende dele;
- contagem de tentativas que sobrevive a devolucao, porque o teto de tentativas depende dela;
- fila de descarte separada, porque "onde foram parar as mensagens que estouraram o limite?" e
  uma pergunta que se faz durante incidente.

Um `dict[str, bytes]` sem versao seria mais curto e faria os testes de idempotencia passarem
sem exercitar nada.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from uuid import uuid4

import structlog

from credit_analysis.domain.armazenamento import Referencia
from credit_analysis.domain.extracao_assincrona import Entrega, PedidoDeExtracao

logger = structlog.get_logger(__name__)


class ArmazenamentoEmMemoria:
    """Guarda em dicionario, com versionamento.

    A versao e um UUID e nao um contador: contador daria a impressao de ordem global, e no S3 o
    version id tambem e opaco. Um teste que dependesse de "versao 2 vem depois da 1" passaria
    aqui e falharia contra o S3.
    """

    __slots__ = ("_objetos",)

    def __init__(self) -> None:
        # `(chave, versao) -> bytes`, e nao `chave -> bytes`. Guardar so a ultima versao faria
        # `obter` devolver o conteudo errado depois de um reenvio, que e precisamente o bug que
        # a referencia versionada existe para impedir.
        self._objetos: dict[tuple[str, str], bytes] = {}

    async def guardar(self, chave: str, conteudo: bytes, tipo_mime: str) -> Referencia:
        versao = uuid4().hex
        self._objetos[(chave, versao)] = conteudo
        logger.debug("armazenamento.guardado", chave=chave, versao=versao, bytes=len(conteudo))
        return Referencia(chave=chave, versao=versao)

    async def obter(self, referencia: Referencia) -> bytes:
        try:
            return self._objetos[(referencia.chave, referencia.versao)]
        except KeyError as exc:
            raise FileNotFoundError(f"objeto ausente: {referencia}") from exc

    @property
    def identificacao(self) -> str:
        return "memoria"

    @property
    def total(self) -> int:
        """Quantos objetos guardados. Usado pelos testes e pelo `/ready`."""
        return len(self._objetos)


class FilaEmMemoria:
    """Fila com visibilidade, contagem de tentativas e descarte.

    ## Por que ha uma nocao de "em voo"

    Uma mensagem consumida sai da fila principal e **nao** e apagada: ela fica em voo ate ser
    confirmada. E o que reproduz a semantica do SQS, onde a mensagem volta a ficar visivel se
    ninguem a confirmar.

    Sem isso, um teste que consome e falha veria a fila vazia, e a garantia "trabalho nao se
    perde quando o trabalhador morre" nunca seria exercitada — que e justamente a garantia pela
    qual se escolhe uma fila.
    """

    __slots__ = ("_descartadas", "_em_voo", "_lock", "_pendentes", "_tentativas", "_teto")

    def __init__(self, teto_de_tentativas: int = 3) -> None:
        self._pendentes: list[PedidoDeExtracao] = []
        self._em_voo: dict[str, PedidoDeExtracao] = {}
        # Contagem por documento e nao por recibo: o recibo muda a cada entrega, e uma contagem
        # por recibo reiniciaria em cada tentativa — o teto nunca seria alcancado.
        self._tentativas: dict[str, int] = defaultdict(int)
        self._descartadas: list[tuple[PedidoDeExtracao, str]] = []
        self._teto = teto_de_tentativas
        self._lock = asyncio.Lock()

    async def publicar(self, pedido: PedidoDeExtracao) -> None:
        async with self._lock:
            self._pendentes.append(pedido)

    async def consumir(self, quantidade: int = 1, espera_segundos: int = 20) -> list[Entrega]:
        # `espera_segundos` e ignorado aqui de proposito: sem rede, nao ha o que esperar, e
        # dormir tornaria a suite lenta sem cobrir nada. O parametro existe no protocolo por
        # causa do long polling do SQS.
        async with self._lock:
            entregas: list[Entrega] = []
            for _ in range(min(quantidade, len(self._pendentes))):
                pedido = self._pendentes.pop(0)
                recibo = uuid4().hex
                self._em_voo[recibo] = pedido
                chave = str(pedido.documento_id)
                self._tentativas[chave] += 1
                entregas.append(
                    Entrega(pedido=pedido, recibo=recibo, tentativas=self._tentativas[chave])
                )
            return entregas

    async def confirmar(self, entrega: Entrega) -> None:
        async with self._lock:
            self._em_voo.pop(entrega.recibo, None)
            self._tentativas.pop(str(entrega.pedido.documento_id), None)

    async def devolver(self, entrega: Entrega, motivo: str) -> None:
        async with self._lock:
            self._em_voo.pop(entrega.recibo, None)
            chave = str(entrega.pedido.documento_id)

            if self._tentativas[chave] >= self._teto:
                # Descarte, e nao devolucao infinita. Um PDF corrompido falha igual nas
                # cinquenta tentativas; sem teto ele ocuparia o trabalhador para sempre.
                self._descartadas.append((entrega.pedido, motivo))
                self._tentativas.pop(chave, None)
                logger.warning(
                    "fila.descartada",
                    documento_id=chave,
                    tentativas=self._teto,
                    motivo=motivo,
                )
                return

            self._pendentes.append(entrega.pedido)

    @property
    def pendentes(self) -> int:
        return len(self._pendentes)

    @property
    def em_voo(self) -> int:
        return len(self._em_voo)

    @property
    def descartadas(self) -> list[tuple[PedidoDeExtracao, str]]:
        """A fila de descarte, legivel.

        Exposta porque "quais documentos estouraram o limite, e por que?" e a pergunta que se faz
        durante incidente — e um teste que so contasse quantas seriam inutil para responde-la.
        """
        return list(self._descartadas)
