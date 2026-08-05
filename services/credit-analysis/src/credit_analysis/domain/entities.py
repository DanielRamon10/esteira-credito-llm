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

from credit_analysis.domain.armazenamento import EstadoDocumento, Referencia
from credit_analysis.domain.documento import OrigemDaRenda
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
    """Um arquivo enviado pelo cliente, em qualquer ponto do fluxo de extracao.

    ## O que a Camada 8 mudou aqui

    O OCR deixou de rodar dentro da requisicao, entao esta entidade passou a existir tambem
    **antes** de haver texto. Isso trouxe dois campos e tirou uma suposicao.

    `estado` substitui o `processado: bool` como fonte de verdade. O booleano colapsava tres
    situacoes distintas em "false" — ainda na fila, falhou por erro tecnico, e reprovado no piso
    de qualidade da POL-002 — e o canal de atendimento nao tinha o que dizer a quem enviou o
    documento. Ele continua existindo por compatibilidade, derivado do estado.

    `referencia` aponta para o objeto guardado, com versao. E o que sustenta a retencao de 5
    anos exigida pela POL-006 secao 5: sem ela, "o documento que embasou este parecer" seria
    apenas um hash, e o conteudo original nao seria recuperavel.
    """

    tipo: TipoDocumento
    nome_arquivo: str
    conteudo_hash: str
    texto_extraido: str | None = None
    confianca_ocr: Percentual | None = None
    estado: EstadoDocumento = EstadoDocumento.RECEBIDO
    referencia: Referencia | None = None
    erro: str | None = None

    # Tentativa de injecao detectada no texto extraido.
    #
    # **Persistido, e nao apenas logado**, e isso mudou com a Camada 8. No fluxo sincrono o sinal
    # ia direto para a resposta HTTP, que o cliente recebia na mesma requisicao. Com 202, aquela
    # resposta nao existe mais — e sem gravar, o unico registro seria a linha de log.
    #
    # Isso nao serviria: o flag e material de revisao humana e de auditoria, e "estava no log de
    # duas semanas atras" nao e uma resposta aceitavel para "este documento tinha injecao?".
    injecao_suspeita: bool = False
    categorias_injecao: tuple[str, ...] = ()

    # Qual motor leu o documento, e se o caso exige um analista.
    #
    # Persistidos pela mesma razao do flag de injecao: no fluxo sincrono iam direto para a
    # resposta HTTP. Com 202 essa resposta nao existe, e sem gravar eles ficariam so no log.
    #
    # `exige_revisao_humana` e decisao de politica (POL-002 secao 3.2) e nao detalhe tecnico:
    # deixa-lo de fora tornaria impossivel responder "por que este caso foi para a fila do
    # analista?" sem reprocessar o documento.
    motor_ocr: str | None = None
    exige_revisao_humana: bool = False

    # A renda que este documento apurou, ou None quando nao apurou nenhuma.
    #
    # Gravada e nao derivada dos `DadoExtraido`, apesar de o numero estar la. Derivar exigiria a
    # API conhecer o **nome** do campo — `salario_liquido` para holerite, `renda_mediana_extrato`
    # para extrato —, e um terceiro tipo de documento amanha acrescentaria um terceiro nome numa
    # regra que mora longe de quem a define.
    #
    # E este e o numero que alimentou o score: guarda-lo aqui e o que permite responder "de onde
    # veio a renda deste parecer?" sem reexecutar a interpretacao.
    renda_comprovada: Dinheiro | None = None

    # **Qual** campo do holerite produziu a renda acima — o liquido ou o bruto.
    #
    # Sem isto, `renda_comprovada` responde "quanto" e nao responde "de que". Os dois nao valem o
    # mesmo: o liquido e o que entra na conta e paga parcela, o bruto e ~20% maior e superestima a
    # capacidade de pagamento.
    #
    # Gravado, e nao apenas usado na decisao de revisao, porque a pergunta e de auditoria: um caso
    # aprovado meses atras precisa poder dizer se a renda que o sustentou era liquida. Deduzir
    # depois exigiria reprocessar a imagem, que pode nao existir mais.
    #
    # `None` para extrato bancario: la a renda e a mediana dos creditos, e nao existe um "bruto"
    # para confundir com ela. Nao e ausencia de informacao — e ausencia da distincao.
    renda_origem: OrigemDaRenda | None = None
    id: UUID = field(default_factory=uuid4)
    submetido_em: datetime = field(default_factory=_agora)

    @property
    def processado(self) -> bool:
        """Mantido, e agora derivado de `estado`.

        Havia codigo lendo isto antes da Camada 8. Deriva-lo em vez de manter dois campos
        independentes evita o modo de falha classico: um atualizado e o outro nao, com o
        booleano dizendo "processado" para um documento em estado `falhou`.
        """
        return self.estado is EstadoDocumento.EXTRAIDO

    def marcar_extraindo(self) -> None:
        """Transicao para `extraindo`, idempotente.

        Idempotente porque entrega de mensagem e *at-least-once*: a mesma extracao pode ser
        iniciada duas vezes, e a segunda nao deve levantar. O que impede trabalho duplicado e a
        checagem de estado terminal em `aplicar_extracao`, nao esta transicao.
        """
        if self.estado is EstadoDocumento.RECEBIDO:
            self.estado = EstadoDocumento.EXTRAINDO

    def concluir_extracao(
        self,
        texto: str,
        confianca: Percentual,
        injecao_suspeita: bool = False,
        categorias_injecao: tuple[str, ...] = (),
        motor_ocr: str | None = None,
        exige_revisao_humana: bool = False,
        renda_comprovada: Dinheiro | None = None,
        renda_origem: OrigemDaRenda | None = None,
    ) -> None:
        self.texto_extraido = texto
        self.confianca_ocr = confianca
        self.estado = EstadoDocumento.EXTRAIDO
        self.erro = None
        self.injecao_suspeita = injecao_suspeita
        self.categorias_injecao = categorias_injecao
        self.motor_ocr = motor_ocr
        self.exige_revisao_humana = exige_revisao_humana
        self.renda_comprovada = renda_comprovada
        self.renda_origem = renda_origem

    def rejeitar_por_qualidade(self, motivo: str, confianca: Percentual) -> None:
        """Reprovado no piso da POL-002. O texto extraido **e preservado**.

        Descarta-lo pareceria mais limpo e destruiria a evidencia: sem ele nao ha como auditar
        por que a rejeicao aconteceu, nem comparar com o reenvio.
        """
        self.confianca_ocr = confianca
        self.estado = EstadoDocumento.REJEITADO
        self.erro = motivo

    def falhar(self, motivo: str) -> None:
        self.estado = EstadoDocumento.FALHOU
        self.erro = motivo


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

    # Pedido de revisao humana da decisao automatizada (LGPD art. 20).
    #
    # ## Por que campos proprios, e nao `reabrir_para_reavaliacao`
    #
    # A reabertura existe para **evidencia nova** e incrementa `reavaliacoes`, que tem teto de 5 —
    # criado para impedir que alguem reenvie documento indefinidamente ate obter o parecer que quer.
    #
    # Reusar aquilo para o art. 20 erraria em tres pontos: limitaria um **direito** a cinco usos,
    # poria a analise em `PROCESSANDO` quando nada esta sendo processado, e confundiria "chegou
    # documento novo" com "o titular contestou".
    #
    # Por isso o pedido e ortogonal ao `status`: ele nao muda o ciclo de vida da analise, nao apaga
    # o parecer e nao altera a decisao. Ele registra que um humano precisa olhar.
    #
    # ## Por que a data e o solicitante, e nao um booleano
    #
    # A data e o que faz o prazo de resposta existir; um booleano diria "houve pedido" sem dizer
    # desde quando, e o prazo da LGPD conta da solicitacao.
    #
    # `revisao_solicitada_por` guarda o **sujeito do token** — o canal de atendimento que registrou
    # o pedido —, e nao o titular: quem pede e sempre o titular, e repetir isso seria guardar dado
    # pessoal para nao dizer nada. O que a trilha precisa saber e por qual canal entrou.
    revisao_solicitada_em: datetime | None = None
    revisao_solicitada_por: str | None = None
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

    def solicitar_revisao_humana(self, canal: str, agora: datetime | None = None) -> bool:
        """Registra pedido de revisao da decisao automatizada. Devolve se este pedido e o primeiro.

        LGPD art. 20: o titular tem direito a pedir revisao de decisao tomada unicamente por
        tratamento automatizado que afete seus interesses — e credito e o exemplo que o proprio
        artigo cita.

        ## Exige parecer, e o erro e de transicao

        Nao se contesta decisao que nao existe. Uma analise em `PENDENTE` ou `PROCESSANDO` ainda nao
        decidiu nada, e aceitar o pedido ali criaria uma trilha de contestacao de nada — pior, o
        prazo de resposta comecaria a contar antes de haver o que responder.

        ## Idempotente, e a primeira data e a que vale

        Pedido repetido nao sobrescreve `revisao_solicitada_em`. O prazo de resposta conta da
        **primeira** solicitacao, e atualizar a data a cada reenvio daria ao controlador um jeito de
        empurrar o prazo para frente reenviando o pedido do proprio titular.

        Devolve `False` no repetido para a rota poder dizer "ja estava em revisao" em vez de sugerir
        que abriu outra.

        ## O que este metodo nao faz

        Nao muda a decisao. Um pedido de revisao que aprovasse automaticamente seria absurdo, e um
        que negasse seria pior; o que ele faz e tirar o caso do caminho automatico. Quem revisa e
        uma pessoa, e o parecer atual continua visivel com a justificativa que o sustenta — que e a
        outra metade do art. 20 (§1: informacao sobre os criterios).
        """
        if self.parecer is None:
            raise TransicaoInvalida(
                "Nao ha decisao a revisar: a analise esta em "
                f"{self.status.value} e nenhum parecer foi emitido. O art. 20 trata de revisao de "
                "decisao tomada, e nao de solicitacao em andamento."
            )

        if self.revisao_solicitada_em is not None:
            return False

        self.revisao_solicitada_em = agora if agora is not None else _agora()
        self.revisao_solicitada_por = canal
        self.atualizada_em = _agora()
        return True

    @property
    def em_revisao_humana(self) -> bool:
        """Se ha pedido de revisao pendente sobre esta analise."""
        return self.revisao_solicitada_em is not None

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
