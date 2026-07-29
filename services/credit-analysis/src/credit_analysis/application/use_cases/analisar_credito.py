"""Caso de uso: submeter e processar uma analise de credito.

O caso de uso orquestra; ele nao calcula. A regra de negocio mora no dominio
(`domain.scoring`), a persistencia mora atras de um port. Isso mantem o
Single Responsibility: se a formula de score mudar, este arquivo nao muda; se
o banco mudar, este arquivo tambem nao muda.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import structlog

from credit_analysis.application.ports import ConsultaBureau, RepositorioAnalises
from credit_analysis.domain import scoring
from credit_analysis.domain.entities import AnaliseCredito, PropostaCredito, Solicitante
from credit_analysis.domain.exceptions import AnaliseNaoEncontrada
from credit_analysis.domain.value_objects import Dinheiro

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ComandoAnalisar:
    """Entrada do caso de uso, ja convertida para tipos de dominio.

    A traducao de JSON cru para value objects acontece na borda (API); daqui
    para dentro tudo ja e valido por construcao.
    """

    solicitante: Solicitante
    proposta: PropostaCredito
    renda_comprovada: Dinheiro | None = None
    meses_historico_bancario: int = 0


class AnalisarCredito:
    """Executa a esteira completa para uma solicitacao."""

    def __init__(self, repositorio: RepositorioAnalises, bureau: ConsultaBureau) -> None:
        self._repositorio = repositorio
        self._bureau = bureau

    async def executar(self, comando: ComandoAnalisar) -> AnaliseCredito:
        analise = AnaliseCredito(solicitante=comando.solicitante, proposta=comando.proposta)

        log = logger.bind(
            analise_id=str(analise.id),
            cpf=comando.solicitante.cpf.mascarado,  # nunca o CPF inteiro
        )
        log.info("analise.iniciada", valor=str(comando.proposta.valor_solicitado))

        analise.iniciar_processamento()
        await self._repositorio.salvar(analise)

        try:
            tem_restricao = await self._bureau.tem_restricao(comando.solicitante.cpf.numero)

            entrada = scoring.EntradaScore(
                solicitante=comando.solicitante,
                proposta=comando.proposta,
                renda_comprovada=comando.renda_comprovada,
                meses_historico_bancario=comando.meses_historico_bancario,
                tem_restricao_cadastral=tem_restricao,
            )
            parecer = scoring.avaliar(entrada)
            analise.concluir(parecer)

            log.info(
                "analise.concluida",
                decisao=parecer.decisao.value,
                score=parecer.score,
                risco=parecer.nivel_risco.value,
            )
        except Exception as exc:
            # Falha de infraestrutura nao pode deixar a analise em PROCESSANDO
            # para sempre; marcamos FALHA, persistimos e propagamos.
            analise.falhar(str(exc))
            await self._repositorio.salvar(analise)
            log.error("analise.falhou", erro=str(exc), exc_info=True)
            raise

        await self._repositorio.salvar(analise)
        return analise


class ConsultarAnalise:
    """Recupera uma analise ja submetida."""

    def __init__(self, repositorio: RepositorioAnalises) -> None:
        self._repositorio = repositorio

    async def executar(self, analise_id: UUID) -> AnaliseCredito:
        analise = await self._repositorio.buscar_por_id(analise_id)
        if analise is None:
            raise AnaliseNaoEncontrada(f"Analise {analise_id} nao encontrada")
        return analise


class ListarAnalises:
    """Paginacao simples sobre o historico de analises."""

    def __init__(self, repositorio: RepositorioAnalises) -> None:
        self._repositorio = repositorio

    async def executar(self, limite: int = 50, offset: int = 0) -> tuple[list[AnaliseCredito], int]:
        itens = await self._repositorio.listar(limite=limite, offset=offset)
        total = await self._repositorio.contar()
        return itens, total
