"""Caso de uso: submeter e processar uma analise de credito.

O caso de uso orquestra; ele nao calcula. A regra de negocio mora no dominio
(`domain.scoring`), a persistencia mora atras de um port. Isso mantem o
Single Responsibility: se a formula de score mudar, este arquivo nao muda; se
o banco mudar, este arquivo tambem nao muda.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

import structlog

from credit_analysis.application.ports import ConsultaBureau, ConsultaKYC, RepositorioAnalises
from credit_analysis.domain import scoring
from credit_analysis.domain.entities import AnaliseCredito, PropostaCredito, Solicitante
from credit_analysis.domain.exceptions import AnaliseNaoEncontrada
from credit_analysis.domain.kyc import ResultadoKYC
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

    def __init__(
        self,
        repositorio: RepositorioAnalises,
        bureau: ConsultaBureau,
        kyc: ConsultaKYC | None = None,
    ) -> None:
        self._repositorio = repositorio
        self._bureau = bureau
        # Opcional: sem servico de conformidade configurado, o gate nao e aplicado.
        # Em producao a ausencia impede a subida (ver `_montar_kyc` em `api/app.py`),
        # entao o `None` aqui cobre desenvolvimento e teste, nao um ambiente real.
        self._kyc = kyc

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
            # As duas consultas externas em paralelo: bureau e KYC nao dependem um do
            # outro, e em serie a analise pagaria a soma das duas latencias. Com
            # `gather` paga a maior.
            tem_restricao, kyc = await asyncio.gather(
                self._bureau.tem_restricao(comando.solicitante.cpf.numero),
                self._consultar_kyc(comando.solicitante),
            )

            entrada = scoring.EntradaScore(
                solicitante=comando.solicitante,
                proposta=comando.proposta,
                renda_comprovada=comando.renda_comprovada,
                meses_historico_bancario=comando.meses_historico_bancario,
                tem_restricao_cadastral=tem_restricao,
            )
            parecer = scoring.avaliar(entrada)

            # O gate so aperta, nunca afrouxa. Aplicado DEPOIS do score de proposito:
            # o parecer registra a nota de credito que o motor deu e, em seguida, a
            # restricao de conformidade — separados, os dois porques ficam legiveis
            # na justificativa.
            if kyc is not None:
                parecer = scoring.aplicar_gate_kyc(parecer, kyc)

            analise.concluir(parecer)

            log.info(
                "analise.concluida",
                decisao=parecer.decisao.value,
                score=parecer.score,
                risco=parecer.nivel_risco.value,
                kyc=kyc.decisao.value if kyc else "nao_configurado",
                kyc_triagem_id=kyc.triagem_id if kyc else None,
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

    async def _consultar_kyc(self, solicitante: Solicitante) -> ResultadoKYC | None:
        """Consulta a triagem, ou devolve None quando nao ha servico configurado.

        Nao ha `try` aqui: o port nao levanta excecao de rede por contrato, e um
        `except` defensivo esconderia a violacao desse contrato em vez de expo-la.
        """
        if self._kyc is None:
            return None
        return await self._kyc.triar(solicitante.nome, solicitante.cpf.numero)


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
