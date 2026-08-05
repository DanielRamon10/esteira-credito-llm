"""Casos de uso do ciclo de vida do dado pessoal: pedido de exclusao e purga por prazo.

## Dois caminhos, e por que nao um

Os dois removem dado pessoal, e a semelhanca engana. Eles diferem no que importa:

|                     | pedido do titular          | purga por prazo             |
|---------------------|----------------------------|-----------------------------|
| gatilho             | pessoa, canal de atendimento| relogio                     |
| escopo              | um CPF                     | tudo que venceu             |
| prazo de resposta   | 15 dias (LGPD art. 19)     | nao ha                      |
| precisa de recibo   | sim, e obrigacao provar     | nao, basta o log            |
| quem chama          | rota autenticada            | job noturno                 |

Unificar num "remover dado" parametrizado misturaria a prova que o controlador deve ao titular com
a rotina de faxina. E o pedido do titular tem uma consequencia que a purga nao tem: ele precisa
devolver **o que foi feito**, item por item, porque quem pediu tem direito a saber.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import structlog

from credit_analysis.application.ports import CicloDeVidaDoDado, RegistroDeChaves
from credit_analysis.domain.retencao import (
    PRAZOS,
    RETENCAO_TEXTO_OCR,
    ClasseDeDado,
)
from credit_analysis.domain.value_objects import CPF

logger = structlog.get_logger(__name__)

# Os dois motivos que o schema aceita. Ficam aqui como constante e nao como string solta porque o
# `CHECK` da coluna recusa qualquer outro valor, e um literal errado no codigo viraria erro de banco
# em producao em vez de erro de tipo no CI.
MOTIVO_PEDIDO = "pedido_do_titular"
MOTIVO_PRAZO = "prazo_vencido"


@dataclass(frozen=True, slots=True)
class ReciboDeApagamento:
    """O que foi feito, para devolver a quem pediu.

    ## Por que um recibo, e nao um 204

    A LGPD art. 18 §VI da o direito de exclusao, e o art. 19 obriga o controlador a **informar** o
    que fez, em 15 dias. Um `204 No Content` cumpriria a exclusao e nao cumpriria a informacao: o
    titular ficaria sem saber quantos registros existiam nem o que sobrou.

    ## `analises_afetadas` com os ids, e nao apenas a contagem

    O id de uma analise apagada nao identifica pessoa por si — e e o que permite ao titular
    conferir que o pedido alcancou os casos que ele conhece, e ao suporte responder a "e aquele meu
    pedido de marco?" sem consultar dado que acabou de ser removido.
    """

    cpf_titular_conferido: bool
    analises_afetadas: tuple[UUID, ...]
    decisoes_conservadas: int
    executado_em: datetime

    @property
    def nada_a_apagar(self) -> bool:
        return not self.analises_afetadas


class ApagarDadoPessoal:
    """Atende pedido de exclusao do titular (LGPD art. 18 §VI).

    ## O que ele apaga, e o que conserva

    Apaga a analise inteira: identificacao, proposta, documentos, dados extraidos e texto de OCR.
    Conserva o registro da decisao sem identificadores, sob a base legal do art. 16 §I — a discussao
    completa esta no cabecalho de `domain/retencao.py`.

    ## O que ele **nao** consegue apagar, e isso precisa ser dito

    O objeto original no armazenamento. A aplicacao nao tem `s3:DeleteObject`, de proposito: sem
    isso, um comprometimento dela apagaria a evidencia dos pareceres. O objeto sai pela regra de
    ciclo de vida do bucket, em ate 365 dias.

    Para um pedido de exclusao, esperar 365 dias **nao e** atendimento. A lacuna e real, esta
    registrada em `RETENCAO_OBJETO` e o desenho para fecha-la e um job de purga com role propria e
    `DeleteObject` restrito ao prefixo. O recibo nao afirma que o objeto foi removido, e essa
    omissao e deliberada: recibo que promete mais do que houve e pior que recibo incompleto.
    """

    def __init__(self, ciclo: CicloDeVidaDoDado) -> None:
        self._ciclo = ciclo

    async def executar(self, cpf: CPF, *, agora: datetime | None = None) -> ReciboDeApagamento:
        momento = agora if agora is not None else datetime.now(UTC)
        analises = await self._ciclo.buscar_por_cpf(cpf)

        apagadas: list[UUID] = []
        conservadas = 0
        for analise in analises:
            tinha_parecer = analise.parecer is not None
            if await self._ciclo.apagar_identificacao(analise.id, MOTIVO_PEDIDO, momento):
                apagadas.append(analise.id)
                conservadas += int(tinha_parecer)

        # O log **nao** carrega o CPF, e essa e a parte que precisa de cuidado: um pedido de
        # exclusao que registra o CPF em log deixa o dado exatamente onde a pessoa pediu para nao
        # estar, num canal que vai para agregador e retencao propria.
        #
        # O que fica registrado e a operacao: quantas analises, quantas decisoes, quando. Para
        # auditar "atendemos o pedido de X?", a prova esta em `decisao_retida.motivo` e
        # `identificacao_removida_em`, no banco, com controle de acesso.
        logger.info(
            "lgpd.apagamento_atendido",
            analises=len(apagadas),
            decisoes_conservadas=conservadas,
            base_legal="LGPD art. 18 §VI; conservacao sob art. 16 §I",
        )

        return ReciboDeApagamento(
            cpf_titular_conferido=True,
            analises_afetadas=tuple(apagadas),
            decisoes_conservadas=conservadas,
            executado_em=momento,
        )


@dataclass(frozen=True, slots=True)
class ResultadoDaPurga:
    """Quantas linhas cada classe de dado perdeu, para o job poder dizer o que fez."""

    textos_purgados: int
    limite_aplicado: datetime
    executada_em: datetime
    # Chaves de idempotencia fora da janela de 24h (Camada 11).
    #
    # Numero separado e nao somado aos textos: as duas purgas tem prazo, base e volume diferentes —
    # 90 dias contra 24 horas —, e um total unico esconderia uma delas parando de funcionar.
    chaves_purgadas: int = 0


class PurgarDadoVencido:
    """Aplica os prazos de `domain/retencao.py`. Chamado pelo job, nao por rota.

    ## Por que so o texto de OCR, por enquanto

    O prazo do texto e 90 dias e ja vence na pratica. O da identificacao e 5 anos, e nenhuma
    analise deste projeto tem cinco anos — implementar a remocao automatica dela agora seria um
    caminho que nenhum teste exercita com dado real, e que ninguem revisaria antes de 2031.

    O que existe e o suficiente para ela: `identificacao_pode_ser_removida` no dominio,
    `apagar_identificacao` no repositorio, e `MOTIVO_PRAZO` para distinguir do pedido do titular. O
    que falta e a varredura, e ela e uma consulta.

    Dizer isso e melhor que uma varredura de 5 anos sem teste: a segunda pareceria pronta.
    """

    def __init__(self, ciclo: CicloDeVidaDoDado, chaves: RegistroDeChaves | None = None) -> None:
        self._ciclo = ciclo
        # Opcional porque a purga de texto e independente da de chaves: um ambiente sem registro de
        # idempotencia — teste que exercita so a retencao — nao deveria ser obrigado a montar um.
        self._chaves = chaves

    async def executar(self, *, agora: datetime | None = None) -> ResultadoDaPurga:
        momento = agora if agora is not None else datetime.now(UTC)
        limite = momento - RETENCAO_TEXTO_OCR

        textos = await self._ciclo.purgar_texto_de_ocr(limite)
        chaves = await self._chaves.purgar_vencidas(momento) if self._chaves is not None else 0

        prazo = PRAZOS[ClasseDeDado.TEXTO_DOCUMENTO]
        logger.info(
            "lgpd.purga_concluida",
            classe=prazo.classe.value,
            base_legal=prazo.base_legal,
            dias=prazo.duracao.days,
            limite=limite.isoformat(),
            linhas=textos,
        )

        return ResultadoDaPurga(
            textos_purgados=textos,
            limite_aplicado=limite,
            executada_em=momento,
            chaves_purgadas=chaves,
        )
