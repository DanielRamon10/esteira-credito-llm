"""Caso de uso: triar um cliente contra as listas restritivas."""

from __future__ import annotations

import time
from dataclasses import dataclass
from uuid import UUID

import structlog

from kyc_compliance.application.ports import RepositorioListas, RepositorioTriagens
from kyc_compliance.domain.triagem import Triagem, avaliar

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ComandoTriar:
    nome: str
    cpf: str


class TriarCliente:
    """Orquestra: le a lista, delega a decisao ao dominio, persiste o registro."""

    def __init__(self, listas: RepositorioListas, repositorio: RepositorioTriagens) -> None:
        self._listas = listas
        self._repositorio = repositorio

    async def executar(self, comando: ComandoTriar) -> Triagem:
        inicio = time.perf_counter()

        # O caso de uso nao decide nada: quem classifica e decide e `domain.triagem`.
        # Esta funcao existe para buscar insumo, medir e persistir — as tres coisas
        # que o dominio nao pode fazer sem deixar de ser puro.
        triagem = avaliar(comando.nome, comando.cpf, self._listas.todas())
        await self._repositorio.salvar(triagem)

        duracao = time.perf_counter() - inicio
        log = logger.bind(
            triagem_id=str(triagem.id),
            # CPF mascarado no log. O completo esta no registro persistido, com
            # controle de acesso; log vaza para agregador, APM e print de tela.
            cpf=triagem.cpf_mascarado,
        )
        log.info(
            "kyc.triagem_concluida",
            decisao=triagem.decisao.value,
            nivel_risco=triagem.nivel_risco.value,
            correspondencias=len(triagem.correspondencias),
            entradas_avaliadas=triagem.entradas_avaliadas,
            procedencia=self._listas.procedencia,
            duracao_ms=int(duracao * 1000),
        )
        if not triagem.aprovado:
            # Nivel warning para o que exige acao humana: e o que vira alerta e
            # metrica, no mesmo padrao do outro servico.
            log.warning("kyc.exige_atencao", decisao=triagem.decisao.value)

        return triagem


class ConsultarTriagem:
    def __init__(self, repositorio: RepositorioTriagens) -> None:
        self._repositorio = repositorio

    async def executar(self, triagem_id: UUID) -> Triagem | None:
        return await self._repositorio.buscar_por_id(triagem_id)


class ListarTriagens:
    def __init__(self, repositorio: RepositorioTriagens) -> None:
        self._repositorio = repositorio

    async def executar(self, limite: int = 50, offset: int = 0) -> tuple[list[Triagem], int]:
        return (
            await self._repositorio.listar(limite=limite, offset=offset),
            await self._repositorio.contar(),
        )
