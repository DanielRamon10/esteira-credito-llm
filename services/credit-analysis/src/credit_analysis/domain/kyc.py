"""O que a esteira de credito sabe sobre KYC — e o que faz quando nao sabe.

Este modulo e o **contrato de dominio** com o servico de conformidade. Ele nao
importa nada de HTTP: o adapter traduz a resposta do `kyc-compliance` para estes
tipos, e o dominio decide a partir deles.

## A decisao que importa aqui nao e o caminho felizmente

Consultar um servico externo introduz um estado que nao existia: **nao sei**. Uma
esteira que so trata "aprovado" e "reprovado" tem tres saidas erradas quando o KYC
esta fora do ar:

- **Falhar aberto** (aprovar sem verificar) e violacao regulatoria. A Circular BCB
  3.978 exige a diligencia; nao a fazer porque um servico caiu nao e defesa.
- **Falhar fechado** (negar) nega credito a um cliente por causa de uma
  indisponibilidade **nossa**. Injusto com quem pediu e caro comercialmente.
- **Ignorar** e a pior das tres: aprova sem registro de que a verificacao nao
  aconteceu, e ninguem descobre ate a auditoria.

A saida correta e uma quarta: **revisao humana**, com a indisponibilidade dita na
justificativa. A analise nao pode ser aprovada automaticamente sem KYC, e nao deve
ser negada por um problema de infraestrutura — entao vai para quem pode decidir com
a informacao que falta.

E o mesmo padrao do escalonamento de OCR e da citacao rejeitada: quando o sistema
nao tem confianca suficiente, ele **diz isso** em vez de escolher um extremo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class DecisaoKYC(StrEnum):
    """Decisao vinda do servico de conformidade.

    Espelha o enum do `kyc-compliance`, e a duplicacao e o preco de nao acoplar os
    dois servicos por uma biblioteca de dominio compartilhada. O adapter faz a
    traducao e **falha explicitamente** diante de um valor desconhecido — se o outro
    servico adicionar um estado, isto aparece como erro e nao como silencio.
    """

    APROVADO = "aprovado"
    # Aprovado sob diligencia reforcada: o caso do PEP. NAO e impedimento.
    APROVADO_COM_DILIGENCIA = "aprovado_com_diligencia"
    REVISAO_MANUAL = "revisao_manual"
    REPROVADO = "reprovado"

    # Estado local, que nao existe no outro servico: a consulta nao foi concluida.
    # Modelado como decisao, e nao como excecao ou `None`, porque a esteira tem de
    # tomar uma decisao **com** essa informacao — e um `None` seria facil de ignorar
    # com um `if kyc:` distraido.
    INDISPONIVEL = "indisponivel"


@dataclass(frozen=True, slots=True)
class ResultadoKYC:
    """Resposta da triagem, do ponto de vista da esteira de credito."""

    decisao: DecisaoKYC
    nivel_risco: str = ""
    justificativas: tuple[str, ...] = field(default=())
    # Identificador da triagem no outro servico. E o que liga o parecer de credito
    # a diligencia que o embasou — sem ele, "esta analise passou por KYC?" nao tem
    # resposta verificavel.
    triagem_id: str | None = None
    motivo_indisponibilidade: str | None = None

    @property
    def impede_aprovacao_automatica(self) -> bool:
        """Se a esteira nao pode aprovar sozinha.

        Inclui `APROVADO_COM_DILIGENCIA` de proposito: a Circular BCB 3.978 art. 27
        exige aprovacao por alcada superior para Pessoa Exposta Politicamente. Uma
        aprovacao automatica de PEP seria justamente o que a regra proibe — mesmo
        que o KYC tenha dito "aprovado".
        """
        return self.decisao is not DecisaoKYC.APROVADO

    @property
    def veta(self) -> bool:
        """Veto duro: so reprovacao explicita em lista de sancoes."""
        return self.decisao is DecisaoKYC.REPROVADO

    @property
    def indisponivel(self) -> bool:
        return self.decisao is DecisaoKYC.INDISPONIVEL

    @classmethod
    def nao_consultado(cls, motivo: str) -> ResultadoKYC:
        """Constroi o estado de indisponibilidade com o motivo registrado.

        O motivo entra na justificativa do parecer. "Nao foi possivel verificar" sem
        dizer por que obriga quem revisa a abrir log de tres servicos.
        """
        return cls(
            decisao=DecisaoKYC.INDISPONIVEL,
            justificativas=(f"Triagem de KYC nao concluida: {motivo}",),
            motivo_indisponibilidade=motivo,
        )
