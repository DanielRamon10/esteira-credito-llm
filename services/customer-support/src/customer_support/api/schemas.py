"""Contrato HTTP do customer-support."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from customer_support.domain.resposta import OrigemDaResposta, Resposta


class PerguntaRequest(BaseModel):
    """Corpo do POST /v1/atendimentos."""

    model_config = ConfigDict(
        str_strip_whitespace=True,
        json_schema_extra={
            "example": {"mensagem": "Quais documentos preciso para comprovar renda?"}
        },
    )

    mensagem: str = Field(min_length=1, max_length=2000)


class FonteResponse(BaseModel):
    id: str
    titulo: str


class AtendimentoResponse(BaseModel):
    """Resposta ao cliente, mais o que a operacao precisa saber.

    Os campos de auditoria (`vazamentos_bloqueados`, `injecao_detectada`) vao no corpo
    porque o consumidor desta API e o canal de atendimento, nao o navegador do
    cliente. E o canal que decide se mostra a resposta, se abre um alerta ou se
    transfere — e sem esses campos ele nao tem como decidir.
    """

    id: UUID
    texto: str
    intencao: str
    origem: OrigemDaResposta = Field(
        description="modelo (passou pelo guard), artigo (texto revisado) ou roteiro (fixo)"
    )
    fontes: list[FonteResponse]
    encaminhada: bool = Field(description="True quando o caso vai para atendente ou ouvidoria")
    protocolo: str | None = Field(
        default=None, description="Somente em reclamacao formal, por exigencia de ouvidoria"
    )
    vazamentos_bloqueados: list[str] = Field(
        default_factory=list,
        description="Categorias de conteudo interno que o guard de divulgacao barrou",
    )
    injecao_detectada: list[str] = Field(
        default_factory=list, description="Padroes de injecao na mensagem do proprio cliente"
    )
    sinais_de_intencao: list[str]
    criada_em: datetime

    @classmethod
    def de_dominio(cls, r: Resposta) -> AtendimentoResponse:
        return cls(
            id=r.id,
            texto=r.texto,
            intencao=r.intencao.value,
            origem=r.origem,
            fontes=[FonteResponse(id=f.id, titulo=f.titulo) for f in r.fontes],
            encaminhada=r.encaminhada,
            protocolo=r.protocolo,
            vazamentos_bloqueados=list(r.vazamentos_bloqueados),
            injecao_detectada=list(r.injecao_detectada),
            sinais_de_intencao=list(r.sinais_de_intencao),
            criada_em=r.criada_em,
        )


class ErroResponse(BaseModel):
    codigo: str
    mensagem: str
    detalhes: list[dict[str, object]] | None = None


class HealthResponse(BaseModel):
    status: str
    servico: str
    versao: str
    ambiente: str
    artigos_carregados: int = 0
    artigos_publicos: int = 0
    llm: str = ""
