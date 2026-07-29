"""Enumeracoes do dominio de analise de credito."""

from enum import StrEnum


class TipoDocumento(StrEnum):
    """Tipos de documento aceitos na esteira de analise."""

    HOLERITE = "holerite"
    EXTRATO_BANCARIO = "extrato_bancario"
    IMPOSTO_RENDA = "imposto_renda"
    CONTRATO_SOCIAL = "contrato_social"
    COMPROVANTE_RESIDENCIA = "comprovante_residencia"


class StatusAnalise(StrEnum):
    """Ciclo de vida de uma analise."""

    PENDENTE = "pendente"
    PROCESSANDO = "processando"
    CONCLUIDA = "concluida"
    FALHA = "falha"


class NivelRisco(StrEnum):
    """Faixa de risco atribuida ao solicitante."""

    BAIXO = "baixo"
    MEDIO = "medio"
    ALTO = "alto"
    CRITICO = "critico"


class Decisao(StrEnum):
    """Recomendacao final da esteira.

    A esteira nunca decide sozinha um caso limitrofe: quando a confianca e baixa
    ou ha divergencia entre as fontes, o caso vai para ANALISE_MANUAL.
    """

    APROVADO = "aprovado"
    APROVADO_COM_RESSALVAS = "aprovado_com_ressalvas"
    NEGADO = "negado"
    ANALISE_MANUAL = "analise_manual"


class OrigemDado(StrEnum):
    """Procedencia de um dado extraido, para fins de auditoria."""

    INFORMADO_CLIENTE = "informado_cliente"
    OCR = "ocr"
    EXTRACAO_LLM = "extracao_llm"
    CALCULADO = "calculado"
