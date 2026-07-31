"""O trabalhador que fecha o ciclo: consome a fila, extrai, aplica.

## Por que ele e um caso de uso e nao um script

Ele contem a decisao mais delicada do fluxo assincrono — **o que fazer quando falha** — e essa
decisao e testavel sem rede, sem fila real e sem Lambda. Enterrada num `__main__.py`, ela
existiria apenas em producao.

## A classificacao de erro e o coracao deste arquivo

Toda falha cai em uma de duas categorias, e a diferenca decide se o trabalho e retentado ou
descartado:

    transitorio    objeto ainda nao visivel, timeout de rede, OCR indisponivel
                   -> devolve para a fila; a proxima tentativa pode dar certo

    permanente     PDF corrompido, contrato de mensagem incompativel, documento
                   que nao esta na analise
                   -> marca `falhou` e confirma; retentar da o mesmo resultado

Classificar errado tem custo nas duas direcoes. Tratar permanente como transitorio ocupa o
trabalhador com trabalho que nunca vai concluir — e num sistema com um documento corrompido na
fila, ele consome a capacidade que os outros precisariam. Tratar transitorio como permanente
manda para revisao humana algo que se resolveria sozinho em cinco segundos.

Foi a mesma escolha feita no cliente do KYC, e la ela ja tinha consequencia medida: 401 do KYC e
permanente porque retentar com credencial invalida gasta as tentativas e abre o disjuntor,
transformando erro de configuracao em "servico indisponivel".
"""

from __future__ import annotations

import asyncio
import time

import structlog

from credit_analysis.application.ports import FilaDeTrabalho, RepositorioAnalises
from credit_analysis.application.use_cases.extracao_assincrona import ExtrairDocumento
from credit_analysis.application.use_cases.processar_documento import (
    AplicarExtracao,
    ComandoAplicarExtracao,
)
from credit_analysis.domain.armazenamento import EstadoDocumento
from credit_analysis.domain.exceptions import AnaliseNaoEncontrada, DadosInsuficientes
from credit_analysis.domain.extracao_assincrona import (
    ContratoIncompativel,
    Entrega,
    PedidoDeExtracao,
)
from credit_analysis.infrastructure.observabilidade import metricas
from credit_analysis.infrastructure.ocr.documentos import ErroLeituraDocumento

logger = structlog.get_logger(__name__)

# Erros que **nao** se resolvem com nova tentativa.
#
# Lista explicita em vez de `except Exception` com heuristica: a categoria default e
# transitorio, porque errar para o lado de retentar e recuperavel e errar para o lado de
# descartar perde trabalho.
ERROS_PERMANENTES: tuple[type[Exception], ...] = (
    # Arquivo ilegivel. As cinquenta tentativas dao o mesmo resultado.
    ErroLeituraDocumento,
    # Mensagem de uma versao que este consumidor nao entende. Ver `VERSAO_DO_CONTRATO`.
    ContratoIncompativel,
    # Documento nao esta na analise, ou a analise nao existe. A recepcao anexa antes de
    # enfileirar, entao isto so acontece se alguem apagou — e retentar nao traz de volta.
    AnaliseNaoEncontrada,
    # Documento sem pagina processavel. E o mesmo arquivo em toda tentativa.
    DadosInsuficientes,
)


class Trabalhador:
    """Consome pedidos de extracao e os processa ate a fila esvaziar.

    Nao e um laco infinito por escolha: `processar_lote` faz uma passada e devolve quantos
    tratou. Quem decide continuar e o `__main__` (laco) ou o teste (uma passada).

    Isso importa para o teste: um trabalhador com `while True` dentro so e testavel com timeout e
    cancelamento, e o teste passa a medir o cancelamento em vez do processamento.
    """

    def __init__(
        self,
        fila: FilaDeTrabalho,
        extrair: ExtrairDocumento,
        aplicar: AplicarExtracao,
        repositorio: RepositorioAnalises,
    ) -> None:
        self._fila = fila
        self._extrair = extrair
        self._aplicar = aplicar
        self._repositorio = repositorio

    async def processar_lote(self, quantidade: int = 1, espera_segundos: int = 20) -> int:
        """Consome ate `quantidade` pedidos. Devolve quantos foram tratados."""
        entregas = await self._fila.consumir(quantidade, espera_segundos)
        for entrega in entregas:
            await self._tratar(entrega)
        return len(entregas)

    async def drenar(self, teto_de_passadas: int = 100) -> int:
        """Processa ate a fila esvaziar. Usado localmente e pelos testes.

        O teto existe porque devolucao realimenta a fila: um documento que falha de forma
        transitoria volta, e sem teto o `drenar` de um teste com falha permanente mal
        classificada nunca retornaria. Ele e uma guarda contra o meu proprio erro, nao contra o
        caso normal.
        """
        total = 0
        for _ in range(teto_de_passadas):
            tratados = await self.processar_lote(quantidade=10, espera_segundos=0)
            if tratados == 0:
                return total
            total += tratados

        logger.warning("trabalhador.teto_de_passadas", teto=teto_de_passadas, tratados=total)
        return total

    async def _tratar(self, entrega: Entrega) -> None:
        pedido = entrega.pedido
        log = logger.bind(
            analise_id=str(pedido.analise_id),
            documento_id=str(pedido.documento_id),
            referencia=str(pedido.referencia),
            tentativa=entrega.tentativas,
            # Propagado da requisicao que originou o pedido: e o que mantem a trilha inteira sob
            # o mesmo identificador quando o trabalho atravessa processos.
            request_id=pedido.request_id,
        )

        inicio = time.perf_counter()
        try:
            extraido = await self._extrair.executar(pedido.referencia, pedido.nome_arquivo)

            resultado = await self._aplicar.executar(
                ComandoAplicarExtracao(
                    analise_id=pedido.analise_id,
                    documento_id=pedido.documento_id,
                    ocr=extraido.ocr,
                    paginas_ignoradas=extraido.paginas_ignoradas,
                )
            )
        except ERROS_PERMANENTES as exc:
            # Confirma **depois** de marcar o documento: com a ordem invertida, uma falha ao
            # gravar o estado deixaria a mensagem fora da fila e o documento em `extraindo` para
            # sempre — invisivel para o alerta de terminal e invisivel para a fila.
            await self._marcar_falha(pedido, f"{type(exc).__name__}: {exc}")
            await self._fila.confirmar(entrega)
            metricas.extracoes.labels(desfecho="falhou").inc()
            log.warning("extracao.falha_permanente", erro=type(exc).__name__, mensagem=str(exc))
            return
        except Exception as exc:
            # Categoria default. Devolve, e a fila decide entre nova tentativa e descarte pelo
            # teto — que e onde o limite mora, e nao aqui.
            await self._fila.devolver(entrega, f"{type(exc).__name__}: {exc}")
            metricas.extracoes.labels(desfecho="transitoria").inc()
            log.warning("extracao.falha_transitoria", erro=type(exc).__name__, exc_info=True)
            return

        await self._fila.confirmar(entrega)

        # `resultado.reaplicacao` e nao `entrega.tentativas > 1`. A primeira versao usava a
        # contagem de tentativas, e estava errada: tentativa > 1 significa que a anterior falhou
        # de forma transitoria, nao que o trabalho ja estava feito. Toda retentativa bem-sucedida
        # seria contada como `ja_aplicada`, e a metrica que existe para detectar trabalhador
        # morrendo antes de confirmar passaria a medir retentativa de OCR.
        if resultado.reaplicacao:
            desfecho = "ja_aplicada"
        elif resultado.documento.estado is EstadoDocumento.REJEITADO:
            desfecho = "rejeitada"
        else:
            desfecho = "aplicada"
        metricas.extracoes.labels(desfecho=desfecho).inc()
        metricas.fila_espera.observe(time.perf_counter() - inicio)

        log.info(
            "extracao.concluida",
            desfecho=desfecho,
            estado=resultado.documento.estado.value,
            revisao_humana=resultado.exige_revisao_humana,
        )

    async def _marcar_falha(self, pedido: PedidoDeExtracao, motivo: str) -> None:
        """Registra a falha no documento, para o `GET` ter o que dizer.

        Se o proprio registro falhar, engole: a alternativa e propagar e transformar a falha
        permanente em transitoria, o que reenfileiraria um trabalho que nao vai concluir. O log
        fica como rastro.
        """
        try:
            analise = await self._repositorio.buscar_por_id(pedido.analise_id)
            if analise is None:
                return
            for documento in analise.documentos:
                if documento.id == pedido.documento_id:
                    documento.falhar(motivo)
                    await self._repositorio.salvar(analise)
                    return
        except Exception:
            logger.error("trabalhador.falha_ao_registrar_falha", exc_info=True)


async def laco(trabalhador: Trabalhador, intervalo_ocioso: float = 1.0) -> None:
    """Laco de execucao do trabalhador local. Nao retorna.

    Separado da classe de proposito: o `while True` fica fora do que se testa, e o teste exercita
    `processar_lote` e `drenar` sem precisar de cancelamento.

    O `sleep` no caso ocioso existe porque a fila em memoria nao tem long polling — com SQS o
    `consumir` ja bloqueia por 20s e este sleep nunca e alcancado.
    """
    while True:
        tratados = await trabalhador.processar_lote(quantidade=10, espera_segundos=20)
        if tratados == 0:
            await asyncio.sleep(intervalo_ocioso)
