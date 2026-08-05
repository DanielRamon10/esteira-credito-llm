"""Idempotencia de submissao: a mesma requisicao nao cria duas analises.

## O defeito que isto corrige

`POST /v1/analises` sem chave cria uma analise por chamada. Clique duplo, retry de cliente HTTP,
reenvio depois de timeout de rede — cada um vira uma analise nova para a mesma pessoa, com uma
consulta a bureau nova.

Em credito isso nao e desperdicio, e dano: consulta de credito duplicada aparece no historico do
proprio cliente. E o modo de falha ficou **mais provavel** com a Camada 8, porque o cliente que
recebe 202 e nao ve resultado imediato tende a reenviar.

## O que este modulo garante, e o que nao garante

Garante: **um recurso por chave**. Duas chamadas com a mesma chave produzem uma analise.

Nao garante resposta byte-identica. A repeticao le o recurso no estado atual e o renderiza de novo,
em vez de devolver um retrato guardado — ver `RegistroDeIdempotencia`, que guarda o **id** e nao o
corpo. A diferenca aparece se a analise mudou no intervalo (um documento anexado, por exemplo), e
nesse caso o estado atual e mais util que o antigo.

## Por que guardar o id e nao o corpo

Guardar o corpo seria o desenho comum, e ele quebraria a Camada 10 em silencio.

A resposta de `POST /v1/analises` carrega nome, CPF, renda e parecer. Guardada aqui, viraria uma
**segunda copia** de dado pessoal, com prazo proprio e fora do alcance de `apagar_identificacao`:
um pedido de exclusao atendido deixaria o titular inteiro numa tabela de cache por mais 24 horas, e
o recibo do art. 19 estaria mentindo.

Guardando so o id, a repeticao le o recurso — e se ele foi apagado, a repeticao responde 404 em vez
de ressuscitar dado excluido. O comportamento certo cai fora do desenho, sem precisar de uma regra.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

# Janela de validade da chave.
#
# 24h e o valor que a maior parte das APIs de pagamento usa, e o raciocinio se aplica aqui: cobre
# retry automatico (segundos), reenvio manual de quem achou que falhou (minutos) e reprocessamento
# de um lote noturno (horas). Alem disso, uma chave repetida provavelmente e outra intencao — ou um
# gerador de chave quebrado, que e melhor descobrir por conflito do que por dedupe silencioso.
JANELA = timedelta(hours=24)

# Quanto tempo uma reivindicacao pode ficar `em_andamento` antes de ser considerada abandonada.
#
# Existe porque processo morre: se a requisicao que reivindicou a chave for interrompida entre o
# `INSERT` e a conclusao, sem este prazo a chave ficaria envenenada pelas 24h da janela, e o cliente
# receberia 409 num pedido que nunca foi concluido.
#
# 120s e o dobro do maior tempo de resposta observado nesta rota (a analise sincrona responde em
# milissegundos; o teto existe para o caso patologico). Curto demais permitiria que duas requisicoes
# legitimas se atropelassem; longo demais devolve a chave envenenada por mais tempo.
PRAZO_DE_ABANDONO = timedelta(seconds=120)

# Tamanho maximo da chave, para nao virar canal de dado.
#
# Sem limite, um cliente poderia usar a chave como campo livre — e ela e indexada, comparada e
# registrada em log. 255 cobre UUID, ULID e hash em hexadecimal com folga.
TAMANHO_MAXIMO_DA_CHAVE = 255


class EstadoDaChave(StrEnum):
    """Onde a reivindicacao esta.

    `EM_ANDAMENTO` e um estado de verdade e nao um detalhe: ele e o que permite responder 409 a uma
    segunda chamada concorrente em vez de deixar as duas processarem.
    """

    EM_ANDAMENTO = "em_andamento"
    CONCLUIDA = "concluida"


@dataclass(frozen=True, slots=True)
class RegistroDeIdempotencia:
    """Uma chave reivindicada, com o que basta para decidir o que fazer na repeticao.

    Sem `corpo` e sem `status_http`, e a ausencia e o ponto — ver o cabecalho deste modulo.
    """

    chave: str
    locatario: str
    impressao: str
    estado: EstadoDaChave
    recurso_id: UUID | None
    criada_em: datetime

    def abandonada(self, agora: datetime) -> bool:
        return (
            self.estado is EstadoDaChave.EM_ANDAMENTO
            and agora - self.criada_em >= PRAZO_DE_ABANDONO
        )


def impressao_do_pedido(corpo: Any) -> str:
    """Impressao digital do corpo, para detectar chave reusada com pedido diferente.

    ## Por que isto existe

    Sem comparar o corpo, uma chave reaproveitada por engano — cliente que fixa a chave por sessao,
    por exemplo — faria o segundo pedido, **diferente**, receber a resposta do primeiro. O cliente
    concluiria que submeteu uma analise de R$ 80.000 e teria recebido a de R$ 45.000.

    Devolver 422 nesse caso e mais util que qualquer alternativa: dedupe silencioso esconde um bug
    de cliente, e processar como pedido novo anula a idempotencia.

    ## `sort_keys` e separadores fixos

    JSON equivalente com ordem de chave diferente e o **mesmo pedido**, e um cliente que serializa
    com `dict` nao ordenado alternaria a impressao entre chamadas — transformando o retry legitimo
    em conflito. A canonicalizacao remove essa fonte de falso conflito.

    SHA-256 e nao o corpo: o corpo tem dado pessoal, e este valor e comparado e registrado.
    """
    canonico = json.dumps(corpo, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonico.encode("utf-8")).hexdigest()
