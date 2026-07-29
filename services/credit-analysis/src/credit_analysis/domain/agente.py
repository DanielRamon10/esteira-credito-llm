"""Trilha de execucao do agente — o artefato de auditoria da Camada 4.

Um agente decide sozinho quais ferramentas chamar e em que ordem. Isso e a
utilidade dele e tambem o problema: sem registro, "por que o sistema respondeu
isso?" nao tem resposta. Num banco a pergunta nao e hipotetica — ela vem do
cliente, do ouvidor e do regulador, e vem meses depois, quando ninguem lembra.

Por isso a resposta do agente nunca e so texto. Ela vem acompanhada da trilha:
quais ferramentas rodaram, com quais argumentos, quanto tempo levaram, o que
falhou e **por que a execucao parou**. Uma resposta interrompida por limite de
passos parece igual a uma resposta completa quando se olha so o texto final —
e nao e a mesma coisa. `motivo_parada` existe para que essa diferenca seja um
dado, e nao uma suposicao de quem le.

Estes tipos ficam no dominio, e nao junto do LangGraph, de proposito: a
exigencia de rastrear uma decisao assistida por IA e da regra de negocio, nao
do framework. Trocar LangGraph por outro orquestrador nao muda o que precisa
ser registrado.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum


class MotivoParada(StrEnum):
    """Por que o agente parou de agir.

    A distincao entre `RESPONDEU` e os outros tres e o que separa uma resposta
    confiavel de uma resposta parcial. Colapsar tudo em "terminou" esconde
    exatamente o caso que precisa de atencao humana.
    """

    RESPONDEU = "respondeu"
    LIMITE_DE_PASSOS = "limite_de_passos"
    TEMPO_ESGOTADO = "tempo_esgotado"
    ERRO = "erro"


@dataclass(frozen=True, slots=True)
class PassoAgente:
    """Uma execucao de ferramenta, do jeito que a auditoria precisa ver.

    Guardamos `resumo` em vez do retorno inteiro: o texto completo de cinco
    trechos de politica repetido em cada passo transforma a trilha num despejo
    ilegivel, e o conteudo integral ja esta no corpus. O que a auditoria precisa
    e reconstruir *o caminho*, e para isso basta saber o que foi consultado e o
    que voltou em linhas gerais.

    `argumentos` sao os **validados**, nao os que o modelo emitiu. Um modelo que
    manda `prazo_meses="48"` (string) tem o valor coagido para 48 antes de
    chegar aqui; registrar o texto cru daria a impressao de que a ferramenta
    recebeu algo que ela nunca recebeu.
    """

    ordem: int
    ferramenta: str
    argumentos: Mapping[str, object]
    resumo: str
    sucesso: bool = True
    duracao_ms: int = 0
    erro: str | None = None


@dataclass(frozen=True, slots=True)
class TrilhaAgente:
    """Resposta do agente somada ao caminho que ele percorreu."""

    resposta: str
    passos: tuple[PassoAgente, ...] = field(default=())
    motivo_parada: MotivoParada = MotivoParada.RESPONDEU
    modelo: str = ""
    duracao_ms: int = 0

    # Categorias de injecao detectadas no **retorno das ferramentas**. Retorno
    # de ferramenta volta para o contexto do modelo, entao e superficie de
    # ataque: dado extraido de documento do cliente pode conter instrucao.
    suspeitas_injecao: tuple[str, ...] = field(default=())

    @property
    def completa(self) -> bool:
        """True apenas quando o agente parou porque terminou de responder."""
        return self.motivo_parada is MotivoParada.RESPONDEU

    @property
    def ferramentas_usadas(self) -> tuple[str, ...]:
        """Ferramentas acionadas, na ordem, com repeticao.

        A repeticao importa: um agente que chama a mesma ferramenta quatro
        vezes seguidas esta em loop, e achatar para um conjunto unico apagaria
        justamente o sintoma.
        """
        return tuple(p.ferramenta for p in self.passos)

    @property
    def falhas(self) -> tuple[PassoAgente, ...]:
        return tuple(p for p in self.passos if not p.sucesso)
