"""Traducao de resultado de caso de uso em metrica.

Este modulo existe para manter uma regra: **`application` e `domain` nao
conhecem Prometheus**. As rotas ja recebem o objeto de dominio pronto — parecer,
fundamentacao, resultado de processamento — e este e o lugar natural para
transformar isso em serie temporal, sem que o caso de uso ganhe uma dependencia
de biblioteca de observabilidade.

Fica em `api/` e nao em `infrastructure/observabilidade/` de proposito: as
funcoes daqui conhecem os tipos de retorno dos casos de uso, e esse acoplamento
pertence a camada que ja os conhece. O modulo de infraestrutura define as
metricas; este decide quando incrementa-las.

Nada aqui levanta excecao para o cliente. Uma falha ao registrar metrica nao
pode derrubar uma analise de credito que ja foi calculada com sucesso — o
sistema de medicao nao tem o direito de quebrar o sistema medido.
"""

from __future__ import annotations

import structlog

from credit_analysis.application.use_cases.extracao_assincrona import DocumentoAceito
from credit_analysis.application.use_cases.processar_documento import ResultadoProcessamento
from credit_analysis.domain.documento import QualidadeExtracao
from credit_analysis.domain.entities import AnaliseCredito
from credit_analysis.domain.politica import Fundamentacao
from credit_analysis.infrastructure.observabilidade import metricas

logger = structlog.get_logger(__name__)


def registrar_parecer(analise: AnaliseCredito) -> None:
    """Distribuicao de decisao, score e comprometimento.

    Sao as metricas que respondem a pergunta de negocio — "a taxa de negativa
    subiu?" — e tambem servem de alarme tecnico: uma mudanca brusca na
    distribuicao de score depois de um deploy indica regressao no motor, e
    aparece aqui antes de aparecer em reclamacao de cliente.
    """
    parecer = analise.parecer
    if parecer is None:
        return

    try:
        metricas.decisoes.labels(
            decisao=parecer.decisao.value, nivel_risco=parecer.nivel_risco.value
        ).inc()
        metricas.score.observe(parecer.score)
        metricas.comprometimento_renda.observe(float(parecer.comprometimento_renda.valor))
    except Exception:
        logger.warning("metricas.falha_ao_registrar", origem="parecer", exc_info=True)


def registrar_fundamentacao(fundamentacao: Fundamentacao) -> None:
    """A metrica de alucinacao prometida na Camada 2.

    `citacoes{estado="rejeitada"}` sobre o total e a taxa de citacao inventada.
    Nao e uma metrica de vaidade: ela sobe quando o modelo comeca a parafrasear —
    depois de uma troca de modelo, de uma mudanca de prompt ou de uma
    atualizacao do Ollama — e essa e a unica forma de descobrir isso sem esperar
    que um analista reclame de um parecer com citacao errada.
    """
    try:
        if fundamentacao.citacoes:
            metricas.citacoes.labels(estado="confirmada").inc(len(fundamentacao.citacoes))
        if fundamentacao.citacoes_rejeitadas:
            metricas.citacoes.labels(estado="rejeitada").inc(len(fundamentacao.citacoes_rejeitadas))
    except Exception:
        logger.warning("metricas.falha_ao_registrar", origem="fundamentacao", exc_info=True)


def registrar_processamento(resultado: ResultadoProcessamento) -> None:
    """Confianca de OCR, motor usado e encaminhamento para revisao humana.

    `ocr_confianca_pct` como histograma e nao media: a medicao da Camada 3
    mostrou que a media esconde o caso que importa. Um documento com 87,8% de
    confianca perdeu o CPF, e outro com 83,9% acertou 23 de 24 lancamentos — a
    distribuicao mostra os dois, a media diria que os dois sao equivalentes.
    """
    try:
        metricas.ocr_extracoes.labels(
            motor=resultado.ocr.motor,
            resultado="revisao" if resultado.exige_revisao_humana else "ok",
        ).inc()
        metricas.ocr_confianca.observe(float(resultado.ocr.confianca.valor))

        if resultado.exige_revisao_humana:
            metricas.revisao_humana.labels(motivo=_motivo_da_revisao(resultado)).inc()
    except Exception:
        logger.warning("metricas.falha_ao_registrar", origem="processamento", exc_info=True)


def _motivo_da_revisao(resultado: ResultadoProcessamento) -> str:
    """Classifica o motivo num dominio fechado de tres valores.

    Fechado porque e label: derivar o motivo de texto livre — a mensagem de erro,
    por exemplo — criaria uma serie temporal por variacao de frase. Os tres
    valores espelham exatamente os tres gatilhos de
    `ResultadoProcessamento.exige_revisao_humana`; se um quarto gatilho aparecer
    la e nao aqui, o caso cai em `outro`, que e o sinal de que esta funcao ficou
    para tras.

    A ordem aqui **nao** e a mesma da propriedade, e de proposito. Lá a ordem e
    irrelevante: qualquer gatilho devolve `True`, e o primeiro a casar
    curto-circuita. Aqui e preciso escolher **um** rotulo, e num documento que
    dispara dois gatilhos a informacao mais acionavel e a tentativa de injecao —
    ela move o caso para a area de fraude, nao apenas para a fila de revisao.
    Nenhum sinal se perde por essa escolha: injecao tem contador proprio
    (`credito_injecao_detectada_total`), com a categoria detalhada.
    """
    if resultado.conteudo is not None and resultado.conteudo.suspeito:
        return "injecao_suspeita"
    if resultado.ocr.qualidade is not QualidadeExtracao.CONFIAVEL:
        return "qualidade_de_extracao"
    if resultado.renda_comprovada is None:
        return "renda_nao_apurada"
    return "outro"


def registrar_recepcao(aceito: DocumentoAceito) -> None:
    """Conta a recepcao de um documento, antes de qualquer extracao.

    Metrica separada de `registrar_processamento` de proposito: a diferenca entre as duas e
    exatamente o que ficou pendente na fila. Um unico contador no fim mediria vazao e nao
    diria nada sobre acumulo — e acumulo silencioso e o modo de falha proprio de fila.

    A subtracao (`recebidos - processados`) e o sinal que o alerta usa.
    """
    try:
        metricas.documentos_recebidos.labels(tipo=aceito.estado.value).inc()
    except Exception:
        logger.warning("metricas.falha_ao_registrar", origem="recepcao", exc_info=True)
