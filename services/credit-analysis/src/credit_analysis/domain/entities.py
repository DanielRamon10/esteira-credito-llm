"""Entidades e agregados do dominio.

`AnaliseCredito` e a raiz do agregado: e o unico ponto por onde o estado da
analise muda, e ela guarda a maquina de estados. Nada fora do dominio deve
escrever em `status` diretamente.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from credit_analysis.domain.enums import (
    Decisao,
    NivelRisco,
    OrigemDado,
    StatusAnalise,
    TipoDocumento,
)
from credit_analysis.domain.exceptions import TransicaoInvalida, ValorInvalido
from credit_analysis.domain.value_objects import CPF, Dinheiro, Percentual


def _agora() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class Solicitante:
    """Quem pede o credito."""

    nome: str
    cpf: CPF
    data_nascimento: datetime
    renda_mensal_declarada: Dinheiro

    def __post_init__(self) -> None:
        if not self.nome.strip():
            raise ValorInvalido("Nome do solicitante nao pode ser vazio")
        if self.idade < 18:
            raise ValorInvalido("Solicitante deve ser maior de idade")

    @property
    def idade(self) -> int:
        hoje = _agora().date()
        nascimento = self.data_nascimento.date()
        anos = hoje.year - nascimento.year
        if (hoje.month, hoje.day) < (nascimento.month, nascimento.day):
            anos -= 1
        return anos


@dataclass(slots=True)
class PropostaCredito:
    """O que esta sendo pedido."""

    valor_solicitado: Dinheiro
    prazo_meses: int
    taxa_juros_mensal: Percentual

    def __post_init__(self) -> None:
        if not self.valor_solicitado.positivo:
            raise ValorInvalido("Valor solicitado deve ser positivo")
        if not 1 <= self.prazo_meses <= 120:
            raise ValorInvalido("Prazo deve estar entre 1 e 120 meses")

    @property
    def parcela_mensal(self) -> Dinheiro:
        """Parcela pela Tabela Price (sistema frances de amortizacao).

        PMT = PV * i / (1 - (1+i)^-n). Com taxa zero cai na divisao simples.
        """
        i = self.taxa_juros_mensal.fracao
        n = self.prazo_meses

        if i == 0:
            return Dinheiro(self.valor_solicitado.valor / Decimal(n))

        fator = (Decimal(1) + i) ** n
        pmt = self.valor_solicitado.valor * i * fator / (fator - Decimal(1))
        return Dinheiro(pmt)

    @property
    def custo_total(self) -> Dinheiro:
        return self.parcela_mensal * self.prazo_meses


@dataclass(slots=True)
class DocumentoSubmetido:
    """Um arquivo enviado pelo cliente, antes ou depois do OCR."""

    tipo: TipoDocumento
    nome_arquivo: str
    conteudo_hash: str
    texto_extraido: str | None = None
    confianca_ocr: Percentual | None = None
    id: UUID = field(default_factory=uuid4)
    submetido_em: datetime = field(default_factory=_agora)

    @property
    def processado(self) -> bool:
        return self.texto_extraido is not None


@dataclass(slots=True)
class DadoExtraido:
    """Um dado com procedencia rastreavel.

    Guardar a origem e a confianca junto do valor e o que permite ao parecer
    dizer "renda de R$ 8.000 lida do holerite com 92% de confianca" em vez de
    apenas "renda de R$ 8.000". Sem isso nao ha auditoria possivel.
    """

    campo: str
    valor: str
    origem: OrigemDado
    confianca: Percentual
    documento_id: UUID | None = None


@dataclass(slots=True)
class Parecer:
    """Resultado da esteira, com justificativa rastreavel."""

    decisao: Decisao
    nivel_risco: NivelRisco
    score: int
    comprometimento_renda: Percentual
    justificativas: list[str] = field(default_factory=list)
    politicas_aplicadas: list[str] = field(default_factory=list)
    limite_recomendado: Dinheiro | None = None

    def __post_init__(self) -> None:
        if not 0 <= self.score <= 1000:
            raise ValorInvalido("Score deve estar entre 0 e 1000")


# Transicoes permitidas. Manter isso como dado (e nao como if espalhado pelos
# metodos) torna a maquina de estados inspecionavel e testavel de uma vez so.
#
# CONCLUIDA volta para PROCESSANDO por um motivo de negocio concreto: a esteira
# emite um parecer preliminar com a renda declarada e o cliente apresenta
# documento depois. Sem essa transicao, documento nenhum poderia ser anexado a
# uma analise ja avaliada — o que inviabilizaria a comprovacao de renda, que e
# justamente o que a POL-002 exige.
#
# A reabertura e contada em `reavaliacoes` para que a trilha de auditoria
# mostre quantas vezes o parecer mudou, e por que.
_TRANSICOES: dict[StatusAnalise, frozenset[StatusAnalise]] = {
    StatusAnalise.PENDENTE: frozenset({StatusAnalise.PROCESSANDO, StatusAnalise.FALHA}),
    StatusAnalise.PROCESSANDO: frozenset({StatusAnalise.CONCLUIDA, StatusAnalise.FALHA}),
    StatusAnalise.CONCLUIDA: frozenset({StatusAnalise.PROCESSANDO}),
    # FALHA e terminal: um erro de infraestrutura nao se resolve reanexando
    # documento, e reprocessar exige uma analise nova com trilha propria.
    StatusAnalise.FALHA: frozenset(),
}

# Teto de reaberturas. Existe para que um cliente nao possa reenviar documento
# indefinidamente ate obter o parecer que quer — cada reavaliacao e uma chance a
# mais de acertar o resultado por tentativa.
MAX_REAVALIACOES = 5


@dataclass(slots=True)
class AnaliseCredito:
    """Raiz do agregado. Concentra o ciclo de vida de uma solicitacao."""

    solicitante: Solicitante
    proposta: PropostaCredito
    documentos: list[DocumentoSubmetido] = field(default_factory=list)
    dados_extraidos: list[DadoExtraido] = field(default_factory=list)
    parecer: Parecer | None = None
    status: StatusAnalise = StatusAnalise.PENDENTE
    erro: str | None = None
    reavaliacoes: int = 0
    motivo_reavaliacao: str | None = None
    id: UUID = field(default_factory=uuid4)
    criada_em: datetime = field(default_factory=_agora)
    atualizada_em: datetime = field(default_factory=_agora)

    def _transicionar(self, destino: StatusAnalise) -> None:
        if destino not in _TRANSICOES[self.status]:
            raise TransicaoInvalida(f"Nao e possivel ir de {self.status} para {destino}")
        self.status = destino
        self.atualizada_em = _agora()

    def iniciar_processamento(self) -> None:
        """Inicia o processamento de uma analise nova.

        Recusa analise ja concluida mesmo que a tabela de transicoes permita
        CONCLUIDA -> PROCESSANDO: aquele caminho existe apenas para
        `reabrir_para_reavaliacao`, que incrementa o contador e registra o
        motivo. Permitir a entrada por aqui deixaria o contador furado e a
        reabertura invisivel na auditoria.
        """
        if self.status is StatusAnalise.CONCLUIDA:
            raise TransicaoInvalida(
                "Analise concluida: use `reabrir_para_reavaliacao` para incorporar nova evidencia"
            )
        self._transicionar(StatusAnalise.PROCESSANDO)

    def concluir(self, parecer: Parecer) -> None:
        self._transicionar(StatusAnalise.CONCLUIDA)
        self.parecer = parecer

    def falhar(self, motivo: str) -> None:
        self._transicionar(StatusAnalise.FALHA)
        self.erro = motivo

    def reabrir_para_reavaliacao(self, motivo: str) -> None:
        """Reabre uma analise concluida para incorporar nova evidencia.

        Chamado quando o cliente apresenta documento apos o parecer preliminar.
        Nao apaga o parecer anterior aqui: quem reabre e obrigado a chamar
        `concluir` com o parecer novo, e o contador registra que houve mudanca.
        """
        if self.reavaliacoes >= MAX_REAVALIACOES:
            raise TransicaoInvalida(
                f"Analise ja foi reavaliada {self.reavaliacoes} vezes "
                f"(limite {MAX_REAVALIACOES}). Abra uma nova solicitacao."
            )

        self._transicionar(StatusAnalise.PROCESSANDO)
        self.reavaliacoes += 1
        self.erro = None
        self.motivo_reavaliacao = motivo

    def anexar_documento(self, documento: DocumentoSubmetido) -> None:
        """Anexa um documento. Permitido antes ou durante o processamento.

        Bloqueado em CONCLUIDA de proposito: anexar documento a uma analise
        fechada sem reabri-la deixaria o parecer descolado da evidencia que o
        sustenta. Quem quer anexar depois chama `reabrir_para_reavaliacao`.
        """
        if self.status not in {StatusAnalise.PENDENTE, StatusAnalise.PROCESSANDO}:
            raise TransicaoInvalida(
                f"Documento nao pode ser anexado com a analise em {self.status.value}; "
                f"reabra a analise para reavaliacao primeiro"
            )
        self.documentos.append(documento)
        self.atualizada_em = _agora()

    def registrar_dado(self, dado: DadoExtraido) -> None:
        self.dados_extraidos.append(dado)
        self.atualizada_em = _agora()

    @property
    def finalizada(self) -> bool:
        return self.status in {StatusAnalise.CONCLUIDA, StatusAnalise.FALHA}
