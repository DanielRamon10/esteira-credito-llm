"""Resposta ao cliente, com procedencia e trilha de decisao."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from customer_support.domain.intencao import Intencao


class OrigemDaResposta(StrEnum):
    """De onde saiu o texto que o cliente recebeu.

    Existe porque as tres origens tem confianca diferente, e quem consome a API
    precisa saber qual foi usada:

    - `modelo` passou pelo guard de divulgacao e foi liberada;
    - `artigo` e o texto revisado por gente, usado quando o guard **bloqueou** a
      prosa do modelo ou quando nao ha LLM disponivel;
    - `roteiro` e texto fixo (encaminhamento, fora de escopo, saudacao).
    """

    MODELO = "modelo"
    ARTIGO = "artigo"
    ROTEIRO = "roteiro"


@dataclass(frozen=True, slots=True)
class Fonte:
    """Artigo citado na resposta."""

    id: str
    titulo: str


@dataclass(frozen=True, slots=True)
class Resposta:
    """O que o cliente recebe, mais o que a auditoria precisa ver."""

    texto: str
    intencao: Intencao
    origem: OrigemDaResposta
    fontes: tuple[Fonte, ...] = field(default=())
    sinais_de_intencao: tuple[str, ...] = field(default=())

    # Encaminhamento a humano. `protocolo` so existe para reclamacao formal, porque e
    # o que a Resolucao CMN 4.860 exige registrar.
    encaminhada: bool = False
    protocolo: str | None = None

    # Vazamentos que o guard de divulgacao barrou. Nao vao para o cliente; vao para o
    # log, para a metrica e para o corpo da resposta da API, que e consumida
    # internamente.
    vazamentos_bloqueados: tuple[str, ...] = field(default=())

    # Categorias de injecao detectadas na mensagem do proprio cliente.
    injecao_detectada: tuple[str, ...] = field(default=())

    id: UUID = field(default_factory=uuid4)
    criada_em: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def houve_bloqueio(self) -> bool:
        return bool(self.vazamentos_bloqueados)
