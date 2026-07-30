"""Traducao de resposta de dominio em metrica."""

from __future__ import annotations

import structlog

from customer_support.domain.intencao import Intencao
from customer_support.domain.resposta import Resposta
from customer_support.infrastructure import metricas

logger = structlog.get_logger(__name__)


def registrar_atendimento(resposta: Resposta, duracao_segundos: float) -> None:
    try:
        metricas.atendimentos.labels(
            intencao=resposta.intencao.value, origem=resposta.origem.value
        ).inc()
        metricas.duracao.labels(origem=resposta.origem.value).observe(duracao_segundos)
        metricas.artigos_recuperados.observe(len(resposta.fontes))

        for categoria in resposta.vazamentos_bloqueados:
            metricas.vazamentos_bloqueados.labels(categoria=categoria).inc()

        # A injecao NAO e contada aqui, e a ausencia e deliberada.
        #
        # Quem conta e o gancho registrado em `plataforma.seguranca` (ver
        # `_medir_injecao` no `app.py`). Na primeira versao os dois contavam, e o
        # resultado apareceu na verificacao: uma unica mensagem com injecao marcava
        # **2** no contador. Mesma classe de erro do scrape duplicado da Camada 5 —
        # duas fontes medindo o mesmo evento, e o painel mentindo por um fator de 2.
        #
        # O gancho ganhou porque ele nao pode ser esquecido: se outro caminho deste
        # servico passar a usar `preparar_conteudo_nao_confiavel`, a deteccao ja e
        # contada. Um `for` aqui contaria apenas o que chega nesta resposta.

        if resposta.encaminhada:
            metricas.encaminhamentos.labels(motivo=_motivo_do_encaminhamento(resposta)).inc()
    except Exception:
        logger.warning("metricas.falha_ao_registrar", origem="atendimento", exc_info=True)


def _motivo_do_encaminhamento(resposta: Resposta) -> str:
    """Classifica o motivo num dominio fechado.

    Fechado porque e label. E a ordem importa: `vazamento` vem antes de `intencao`
    porque um encaminhamento causado pelo guard de divulgacao e um evento tecnico que
    precisa ser distinguido do encaminhamento normal de uma reclamacao — sao filas
    diferentes, com donos diferentes.
    """
    if resposta.vazamentos_bloqueados:
        return "vazamento_sem_texto_seguro"
    if resposta.intencao is Intencao.RECLAMACAO:
        return "ouvidoria"
    if resposta.intencao is Intencao.CASO_ESPECIFICO:
        return "atendente"
    return "sem_resposta_na_base"
