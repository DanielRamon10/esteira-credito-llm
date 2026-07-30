"""Contrato HTTP do kyc-compliance.

Separado das entidades pelo mesmo motivo do outro servico: contrato e modelagem
interna evoluem em ritmos diferentes, e expor a entidade faz de qualquer
refatoracao de dominio um breaking change.

Aqui isso tem um peso extra: **este servico e consumido por outro servico**. O
`credit-analysis` depende deste contrato, entao alterar um campo nao quebra apenas
um cliente externo hipotetico — quebra um deploy vizinho no mesmo monorepo.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from kyc_compliance.domain.matching import Correspondencia
from kyc_compliance.domain.triagem import DecisaoKYC, NivelRiscoKYC, Triagem


def _cpf_valido(bruto: str) -> str:
    """Valida CPF com digito verificador.

    ## Sobre a duplicacao com o credit-analysis

    Esta funcao repete a validacao que existe no outro servico, e a repeticao e
    uma decisao — nao um esquecimento.

    Compartilhar o value object exigiria uma biblioteca comum, e biblioteca comum
    de **dominio** entre bounded contexts e o acoplamento que DDD alerta contra:
    os dois contextos evoluem por pressoes diferentes. Este servico vai precisar de
    CNPJ e de documento estrangeiro (lista de sancoes internacional nao tem CPF); o
    outro nao. No dia em que um mudar, o compartilhamento forcaria o outro a
    acompanhar ou a ganhar um parametro condicional.

    O que **vale** compartilhar e infraestrutura tecnica — logging, metricas,
    deteccao de injecao. Isso esta anotado como divida no README do monorepo, para
    ser extraido quando o terceiro servico mostrar o que de fato se repete.
    """
    digitos = "".join(c for c in bruto if c.isdigit())

    if len(digitos) != 11:
        raise ValueError("CPF deve ter 11 digitos")
    if digitos == digitos[0] * 11:
        raise ValueError("CPF com todos os digitos iguais e invalido")

    for tamanho in (9, 10):
        soma = sum(int(digitos[i]) * (tamanho + 1 - i) for i in range(tamanho))
        resto = (soma * 10) % 11
        esperado = 0 if resto == 10 else resto
        if esperado != int(digitos[tamanho]):
            raise ValueError("Digito verificador do CPF invalido")

    return bruto


class TriagemRequest(BaseModel):
    """Corpo do POST /v1/triagens."""

    model_config = ConfigDict(
        str_strip_whitespace=True,
        json_schema_extra={"example": {"nome": "Jose da Silva Junior", "cpf": "529.982.247-25"}},
    )

    nome: str = Field(min_length=3, max_length=200)
    cpf: str = Field(description="Com ou sem pontuacao")

    @field_validator("cpf")
    @classmethod
    def _validar(cls, v: str) -> str:
        return _cpf_valido(v)


class CorrespondenciaResponse(BaseModel):
    """Um casamento, com o *porque* dele.

    `tokens_casados` e `tokens_ausentes` vao no contrato de proposito: sem eles o
    consumidor recebe um score e nao tem como explicar a decisao a um analista ou
    a um regulador — que e a exigencia central deste dominio.
    """

    nome_na_lista: str
    score: float = Field(ge=0, le=1)
    nivel: str
    cpf_confere: bool
    tokens_casados: list[str]
    tokens_ausentes: list[str]
    justificativa: str

    @classmethod
    def de_dominio(cls, c: Correspondencia) -> CorrespondenciaResponse:
        return cls(
            nome_na_lista=c.nome_na_lista,
            score=round(c.score, 4),
            nivel=c.nivel.value,
            cpf_confere=c.cpf_confere,
            tokens_casados=list(c.tokens_casados),
            tokens_ausentes=list(c.tokens_ausentes),
            justificativa=c.justificativa,
        )


class TriagemResponse(BaseModel):
    """Resultado de uma triagem."""

    id: UUID
    decisao: DecisaoKYC
    nivel_risco: NivelRiscoKYC
    aprovado: bool = Field(
        description=(
            "True para aprovado e aprovado_com_diligencia. Diligencia reforcada "
            "NAO e impedimento — e o caso do PEP."
        )
    )
    nome_consultado: str
    cpf_mascarado: str
    correspondencias: list[CorrespondenciaResponse]
    justificativas: list[str]
    entradas_avaliadas: int
    criada_em: datetime

    @classmethod
    def de_dominio(cls, t: Triagem) -> TriagemResponse:
        return cls(
            id=t.id,
            decisao=t.decisao,
            nivel_risco=t.nivel_risco,
            aprovado=t.aprovado,
            nome_consultado=t.nome_consultado,
            # CPF mascarado, nunca completo: a resposta acaba em log de proxy, APM
            # e navegador do analista.
            cpf_mascarado=t.cpf_mascarado,
            correspondencias=[CorrespondenciaResponse.de_dominio(c) for c in t.correspondencias],
            justificativas=list(t.justificativas),
            entradas_avaliadas=t.entradas_avaliadas,
            criada_em=t.criada_em,
        )


class PaginaTriagens(BaseModel):
    itens: list[TriagemResponse]
    total: int
    limite: int
    offset: int


class ErroResponse(BaseModel):
    codigo: str
    mensagem: str
    detalhes: list[dict[str, object]] | None = None


class HealthResponse(BaseModel):
    """Corpo comum de `/health` e `/ready`.

    ## Por que os dois ultimos campos sao `None` e nao `0`/`""`

    Eles eram `int = 0` e `str = ""`, e isso produzia uma **afirmacao falsa** no
    `/health` — descoberta rodando o pod num cluster de verdade:

        GET /health -> {"status":"ok", ..., "entradas_carregadas":0, "procedencia_listas":""}
        GET /ready  -> {"status":"ok", ..., "entradas_carregadas":15, "procedencia_listas":"..."}

    O `/health` nao toca no repositorio de proposito (uma sonda de liveness que consulta
    dependencia transforma lentidao da dependencia em restart loop), entao ele nunca teve
    esse numero para dar. Mas o default preenchia `0` — e num servico cujo pior modo de
    falha e exatamente lista vazia, dizer `0` na sonda de saude e pior que nao dizer
    nada. Quem lesse `/health` durante um incidente concluiria o oposto da verdade.

    Com `None` mais `response_model_exclude_none=True` na rota, os campos simplesmente
    nao aparecem em `/health`. A ausencia e honesta: aquela sonda nao sabe. Quem quer
    saber consulta `/ready`, que sabe.

    A exclusao mora na **rota** e nao aqui: `model_config` nao tem como controlar a
    serializacao que o FastAPI faz do `response_model`, e um `json_schema_extra` com
    `exclude_none` afetaria apenas o OpenAPI — o corpo continuaria saindo com os zeros,
    agora com um schema mentindo sobre isso.
    """

    status: str
    servico: str
    versao: str
    ambiente: str
    # Quantas entradas de lista estao carregadas. Vai no `/ready` de proposito: zero
    # entradas significa que o servico aprovaria todo mundo, e isso precisa ser visivel
    # numa sonda e nao apenas num log de boot.
    entradas_carregadas: int | None = None
    procedencia_listas: str | None = None
