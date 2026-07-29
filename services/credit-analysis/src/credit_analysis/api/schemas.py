"""Schemas de entrada e saida da API.

Os schemas Pydantic sao deliberadamente separados das entidades de dominio.
Parece duplicacao, mas nao e: o contrato HTTP e a modelagem interna evoluem em
ritmos diferentes, e expor a entidade direto significa que qualquer refatoracao
de dominio vira breaking change para os consumidores da API.

Aqui tambem mora a traducao entre tipos primitivos (JSON) e value objects.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from credit_analysis.application.use_cases.processar_documento import ResultadoProcessamento
from credit_analysis.domain.agente import PassoAgente, TrilhaAgente
from credit_analysis.domain.documento import CampoExtraido, QualidadeExtracao
from credit_analysis.domain.entities import AnaliseCredito, Parecer, PropostaCredito, Solicitante
from credit_analysis.domain.enums import Decisao, NivelRisco, StatusAnalise, TipoDocumento
from credit_analysis.domain.exceptions import ValorInvalido
from credit_analysis.domain.extrato import ResumoExtrato
from credit_analysis.domain.politica import Citacao, Fundamentacao, TrechoRecuperado
from credit_analysis.domain.value_objects import CPF, Dinheiro, Percentual


class SolicitanteEntrada(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    nome: str = Field(min_length=2, max_length=200, examples=["Maria Oliveira Santos"])
    cpf: str = Field(examples=["529.982.247-25"], description="Com ou sem pontuacao")
    data_nascimento: datetime = Field(examples=["1990-05-14T00:00:00Z"])
    renda_mensal_declarada: Decimal = Field(gt=0, examples=["8500.00"])

    @field_validator("cpf")
    @classmethod
    def _validar_cpf(cls, v: str) -> str:
        # Valida cedo, na borda: um CPF invalido vira 422 com mensagem clara
        # em vez de estourar como 500 la dentro do caso de uso.
        try:
            CPF(v)
        except ValorInvalido as exc:
            raise ValueError(str(exc)) from exc
        return v

    def para_dominio(self) -> Solicitante:
        return Solicitante(
            nome=self.nome,
            cpf=CPF(self.cpf),
            data_nascimento=self.data_nascimento,
            renda_mensal_declarada=Dinheiro(self.renda_mensal_declarada),
        )


class PropostaEntrada(BaseModel):
    valor_solicitado: Decimal = Field(gt=0, le=Decimal("10000000"), examples=["45000.00"])
    prazo_meses: int = Field(ge=1, le=120, examples=[36])
    taxa_juros_mensal: Decimal = Field(ge=0, le=20, examples=["1.99"])

    def para_dominio(self) -> PropostaCredito:
        return PropostaCredito(
            valor_solicitado=Dinheiro(self.valor_solicitado),
            prazo_meses=self.prazo_meses,
            taxa_juros_mensal=Percentual(self.taxa_juros_mensal),
        )


class AnaliseRequest(BaseModel):
    """Corpo do POST /v1/analises."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "solicitante": {
                    "nome": "Maria Oliveira Santos",
                    "cpf": "529.982.247-25",
                    "data_nascimento": "1990-05-14T00:00:00Z",
                    "renda_mensal_declarada": "8500.00",
                },
                "proposta": {
                    "valor_solicitado": "45000.00",
                    "prazo_meses": 36,
                    "taxa_juros_mensal": "1.99",
                },
                "renda_comprovada": "8200.00",
                "meses_historico_bancario": 18,
            }
        }
    )

    solicitante: SolicitanteEntrada
    proposta: PropostaEntrada
    renda_comprovada: Decimal | None = Field(default=None, ge=0)
    meses_historico_bancario: int = Field(default=0, ge=0, le=600)

    @model_validator(mode="after")
    def _coerencia(self) -> Self:
        if self.renda_comprovada is not None and self.renda_comprovada == 0:
            raise ValueError("renda_comprovada, quando informada, deve ser maior que zero")
        return self


class ParecerResponse(BaseModel):
    decisao: Decisao
    nivel_risco: NivelRisco
    score: int = Field(ge=0, le=1000)
    comprometimento_renda_pct: Decimal
    limite_recomendado: Decimal | None
    justificativas: list[str]
    politicas_aplicadas: list[str]

    @classmethod
    def de_dominio(cls, parecer: Parecer) -> ParecerResponse:
        return cls(
            decisao=parecer.decisao,
            nivel_risco=parecer.nivel_risco,
            score=parecer.score,
            comprometimento_renda_pct=parecer.comprometimento_renda.valor,
            limite_recomendado=(
                parecer.limite_recomendado.valor if parecer.limite_recomendado else None
            ),
            justificativas=parecer.justificativas,
            politicas_aplicadas=parecer.politicas_aplicadas,
        )


class AnaliseResponse(BaseModel):
    """Representacao publica de uma analise.

    O CPF sai mascarado: a resposta pode acabar em log de proxy, APM ou
    navegador do atendente, e nenhum desses e lugar de dado pessoal completo.
    """

    id: UUID
    status: StatusAnalise
    nome_solicitante: str
    cpf_mascarado: str
    valor_solicitado: Decimal
    prazo_meses: int
    parcela_mensal: Decimal
    parecer: ParecerResponse | None
    erro: str | None
    criada_em: datetime
    atualizada_em: datetime

    @classmethod
    def de_dominio(cls, analise: AnaliseCredito) -> AnaliseResponse:
        return cls(
            id=analise.id,
            status=analise.status,
            nome_solicitante=analise.solicitante.nome,
            cpf_mascarado=analise.solicitante.cpf.mascarado,
            valor_solicitado=analise.proposta.valor_solicitado.valor,
            prazo_meses=analise.proposta.prazo_meses,
            parcela_mensal=analise.proposta.parcela_mensal.valor,
            parecer=(ParecerResponse.de_dominio(analise.parecer) if analise.parecer else None),
            erro=analise.erro,
            criada_em=analise.criada_em,
            atualizada_em=analise.atualizada_em,
        )


class PaginaAnalises(BaseModel):
    itens: list[AnaliseResponse]
    total: int
    limite: int
    offset: int


class ConsultaPoliticaRequest(BaseModel):
    """Corpo do POST /v1/politicas/consultar."""

    model_config = ConfigDict(
        str_strip_whitespace=True,
        json_schema_extra={
            "example": {
                "pergunta": "Qual o teto de comprometimento de renda para CDC?",
                "produto": "cdc",
            }
        },
    )

    pergunta: str = Field(min_length=5, max_length=1000)
    produto: str | None = Field(
        default=None, description="Restringe a busca as politicas do produto"
    )


class TrechoRecuperadoResponse(BaseModel):
    """Um trecho devolvido pela busca, com a procedencia do match."""

    politica_id: str
    versao: str
    secao: str
    titulo_politica: str
    texto: str
    score: float
    origem: str = Field(description="denso, lexical, ou a fusao dos dois")

    @classmethod
    def de_dominio(cls, recuperado: TrechoRecuperado) -> TrechoRecuperadoResponse:
        return cls(
            politica_id=recuperado.referencia.politica_id,
            versao=recuperado.referencia.versao,
            secao=recuperado.referencia.secao,
            titulo_politica=recuperado.trecho.titulo_politica,
            texto=recuperado.trecho.texto,
            score=recuperado.score,
            origem=recuperado.origem,
        )


class CitacaoResponse(BaseModel):
    politica_id: str
    versao: str
    secao: str
    trecho_citado: str

    @classmethod
    def de_dominio(cls, citacao: Citacao) -> CitacaoResponse:
        return cls(
            politica_id=citacao.referencia.politica_id,
            versao=citacao.referencia.versao,
            secao=citacao.referencia.secao,
            trecho_citado=citacao.trecho_citado,
        )


class FundamentacaoResponse(BaseModel):
    """Resposta fundamentada, com o resultado da verificacao de citacoes."""

    texto: str
    confiavel: bool = Field(
        description=(
            "True quando ha ao menos uma citacao confirmada e nenhuma rejeitada. "
            "False exige revisao humana antes de usar a resposta."
        )
    )
    citacoes: list[CitacaoResponse]
    citacoes_rejeitadas: list[str] = Field(
        description="Citacoes alegadas pelo modelo que nao foram confirmadas no corpus"
    )
    politicas_consultadas: list[str]

    @classmethod
    def de_dominio(cls, fundamentacao: Fundamentacao) -> FundamentacaoResponse:
        return cls(
            texto=fundamentacao.texto,
            confiavel=fundamentacao.confiavel,
            citacoes=[CitacaoResponse.de_dominio(c) for c in fundamentacao.citacoes],
            citacoes_rejeitadas=list(fundamentacao.citacoes_rejeitadas),
            politicas_consultadas=[str(r) for r in fundamentacao.trechos_consultados],
        )


class CampoExtraidoResponse(BaseModel):
    """Campo lido do documento, com a linha de origem para conferencia."""

    nome: str
    valor: str
    trecho_origem: str
    confianca_pct: Decimal

    @classmethod
    def de_dominio(cls, campo: CampoExtraido) -> CampoExtraidoResponse:
        return cls(
            nome=campo.nome,
            valor=campo.valor_bruto,
            trecho_origem=campo.trecho_origem,
            confianca_pct=campo.confianca.valor,
        )


class ResumoExtratoResponse(BaseModel):
    """Indicadores derivados do extrato bancario."""

    meses_analisados: int
    renda_mediana_mensal: Decimal
    renda_media_mensal: Decimal
    despesa_media_mensal: Decimal
    volatilidade_renda_pct: Decimal
    renda_estavel: bool
    meses_com_saldo_negativo: int
    creditos_recorrentes: list[str]

    @classmethod
    def de_dominio(cls, resumo: ResumoExtrato) -> ResumoExtratoResponse:
        return cls(
            meses_analisados=resumo.meses_analisados,
            renda_mediana_mensal=resumo.renda_mediana_mensal.valor,
            renda_media_mensal=resumo.renda_media_mensal.valor,
            despesa_media_mensal=resumo.despesa_media_mensal.valor,
            volatilidade_renda_pct=resumo.volatilidade_renda.valor,
            renda_estavel=resumo.renda_estavel,
            meses_com_saldo_negativo=resumo.meses_com_saldo_negativo,
            creditos_recorrentes=list(resumo.creditos_recorrentes),
        )


class DocumentoProcessadoResponse(BaseModel):
    """Resultado da extracao de um documento."""

    documento_id: UUID
    analise_id: UUID
    tipo: TipoDocumento
    nome_arquivo: str
    conteudo_hash: str

    motor_ocr: str
    confianca_ocr_pct: Decimal
    qualidade: QualidadeExtracao
    correcoes_aplicadas: list[str]

    renda_comprovada: Decimal | None
    campos_extraidos: list[CampoExtraidoResponse]
    campos_nao_reconhecidos: list[str]
    resumo_extrato: ResumoExtratoResponse | None
    transacoes_rejeitadas: int
    paginas_ignoradas: int

    exige_revisao_humana: bool
    injecao_suspeita: bool = Field(
        description=(
            "True quando o documento contem padrao de tentativa de injecao de "
            "prompt. O valor usado no score vem da extracao estrutural, nao do "
            "LLM, entao a tentativa nao altera o calculo — mas o caso vai para "
            "revisao humana."
        )
    )
    categorias_suspeitas: list[str]

    @classmethod
    def de_dominio(cls, resultado: ResultadoProcessamento) -> DocumentoProcessadoResponse:
        extracao = resultado.extracao_holerite
        campos = (
            [
                CampoExtraidoResponse.de_dominio(c)
                for c in (
                    extracao.cpf,
                    extracao.nome,
                    extracao.empregador,
                    extracao.competencia,
                    extracao.salario_base,
                    extracao.salario_liquido,
                )
                if c is not None
            ]
            if extracao is not None
            else []
        )

        conteudo = resultado.conteudo

        return cls(
            documento_id=resultado.documento.id,
            analise_id=resultado.analise.id,
            tipo=resultado.documento.tipo,
            nome_arquivo=resultado.documento.nome_arquivo,
            conteudo_hash=resultado.documento.conteudo_hash,
            motor_ocr=resultado.ocr.motor,
            confianca_ocr_pct=resultado.ocr.confianca.valor,
            qualidade=resultado.ocr.qualidade,
            correcoes_aplicadas=list(resultado.ocr.correcoes_aplicadas),
            renda_comprovada=(
                resultado.renda_comprovada.valor if resultado.renda_comprovada else None
            ),
            campos_extraidos=campos,
            campos_nao_reconhecidos=(
                list(extracao.campos_nao_reconhecidos) if extracao is not None else []
            ),
            resumo_extrato=(
                ResumoExtratoResponse.de_dominio(resultado.resumo_extrato)
                if resultado.resumo_extrato is not None
                else None
            ),
            transacoes_rejeitadas=resultado.transacoes_rejeitadas,
            paginas_ignoradas=resultado.paginas_ignoradas,
            exige_revisao_humana=resultado.exige_revisao_humana,
            injecao_suspeita=bool(conteudo and conteudo.suspeito),
            categorias_suspeitas=list(conteudo.categorias) if conteudo else [],
        )


class PerguntaAgenteRequest(BaseModel):
    """Corpo do POST /v1/agente/consultar."""

    model_config = ConfigDict(
        str_strip_whitespace=True,
        json_schema_extra={
            "example": {
                "pergunta": "O comprometimento deste caso passa do teto? O que a politica exige?",
                "analise_id": "8c1f9e6a-0b3d-4a2e-9f11-6c5d4e3b2a10",
            }
        },
    )

    pergunta: str = Field(min_length=3, max_length=2000)
    analise_id: UUID | None = Field(
        default=None,
        description=(
            "Caso a discutir. Fixa qual analise o agente pode ler — ele nao "
            "escolhe, nem pode trocar de caso durante o atendimento."
        ),
    )


class PassoAgenteResponse(BaseModel):
    """Uma ferramenta executada pelo agente."""

    ordem: int
    ferramenta: str
    argumentos: dict[str, object]
    resumo: str
    sucesso: bool
    duracao_ms: int
    erro: str | None = None

    @classmethod
    def de_dominio(cls, passo: PassoAgente) -> PassoAgenteResponse:
        return cls(
            ordem=passo.ordem,
            ferramenta=passo.ferramenta,
            argumentos=dict(passo.argumentos),
            resumo=passo.resumo,
            sucesso=passo.sucesso,
            duracao_ms=passo.duracao_ms,
            erro=passo.erro,
        )


class TrilhaAgenteResponse(BaseModel):
    """Resposta do agente com o caminho percorrido.

    A trilha vai no corpo, e nao so no log, porque quem consome precisa poder
    decidir o que fazer com a resposta. `completa=false` significa que o agente
    parou por limite ou por tempo — a resposta ainda serve, mas nao deve ser
    tratada como conclusiva.
    """

    resposta: str
    completa: bool = Field(
        description="False quando o agente parou por limite de passos, tempo ou erro"
    )
    motivo_parada: str
    modelo: str
    duracao_ms: int
    passos: list[PassoAgenteResponse] = Field(default_factory=list)
    ferramentas_usadas: list[str] = Field(
        default_factory=list,
        description="Na ordem, com repeticao — repeticao e o sintoma de loop",
    )
    injecao_suspeita: bool = Field(
        default=False,
        description="Padrao de injecao detectado no retorno de alguma ferramenta",
    )
    categorias_suspeitas: list[str] = Field(default_factory=list)

    @classmethod
    def de_dominio(cls, trilha: TrilhaAgente) -> TrilhaAgenteResponse:
        return cls(
            resposta=trilha.resposta,
            completa=trilha.completa,
            motivo_parada=trilha.motivo_parada.value,
            modelo=trilha.modelo,
            duracao_ms=trilha.duracao_ms,
            passos=[PassoAgenteResponse.de_dominio(p) for p in trilha.passos],
            ferramentas_usadas=list(trilha.ferramentas_usadas),
            injecao_suspeita=bool(trilha.suspeitas_injecao),
            categorias_suspeitas=list(trilha.suspeitas_injecao),
        )


class ErroResponse(BaseModel):
    """Formato unico de erro da API.

    Um shape so para todo erro facilita a vida de quem consome: o cliente
    trata `codigo` programaticamente e mostra `mensagem` para o usuario.
    """

    codigo: str
    mensagem: str
    detalhes: list[dict[str, object]] | None = None


class HealthResponse(BaseModel):
    status: str
    servico: str
    versao: str
    ambiente: str
