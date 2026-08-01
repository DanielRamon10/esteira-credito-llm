"""Entrypoint do trabalhador de extracao como processo separado.

    python -m credit_analysis.trabalhador_main

## ESTE ENTRYPOINT NAO FUNCIONA HOJE, e ele diz isso ao subir

Falta a dependencia que ele exige: um repositorio de analise **compartilhado** entre processos. A
unica implementacao de `RepositorioAnalises` e a em memoria, e o schema do Postgres tem apenas
`trecho_politica` — a tabela do RAG. Nao existem `analise`, `documento` nem `dado_extraido`.

Com isso, este processo veria o **seu proprio** repositorio, vazio. A API anexa o documento no dela
e publica na fila; o trabalhador consome e nao acha o documento. `AplicarExtracao` classifica isso
como erro permanente — corretamente, porque retentar nao faz o documento aparecer — e **todo**
documento iria para `falhou`.

Nao e detalhe de configuracao: e uma dependencia que nao existe.

## Por que o arquivo existe, entao

O desenho ao redor dele esta pronto e verificado: os tres casos de uso, os adapters de S3 e SQS, a
classificacao de erro entre transitorio e permanente. O que falta e uma peca — o repositorio
compartilhado — e ela e o proximo passo natural do projeto, nao um ajuste desta camada.

Apagar o arquivo esconderia esse desenho. Deixa-lo subir em silencio seria pior: ele consumiria a
fila e mandaria todo documento para `falhou`, e o sintoma ("a extracao nunca funciona") nao
apontaria para a causa.

O que **funciona** hoje e o trabalhador no mesmo processo da API, via
`CREDIT_TRABALHADOR_EM_PROCESSO`. Ele compartilha o repositorio por estar no mesmo espaco de
memoria, e tem outra limitacao, documentada la: uma replica so.

## Por que valeria a pena, quando o repositorio existir

1. **Escala independente.** A API atende requisicoes de milissegundos; a extracao leva segundos e
   pode chamar modelo de visao. Juntos, dimensionar um significa dimensionar o outro — e o HPA
   escalaria a API inteira porque a fila cresceu.
2. **Falha isolada.** Um OCR que estoura memoria mata o processo. Sendo o mesmo da API, leva as
   requisicoes em voo junto.
3. **Pool de conexao.** Uma extracao longa segura conexao que a API precisaria atender com.

## E por que ele nao teria servidor HTTP

Sonda de Kubernetes para consumidor de fila e outra coisa: o processo estar vivo nao diz que ele
esta consumindo, e um 200 num trabalhador travado e pior que nada. O sinal certo e a profundidade
da fila, que o Prometheus le do proprio SQS.
"""

from __future__ import annotations

import structlog
from plataforma.logging import configurar_logging

from credit_analysis.config import Settings, get_settings

logger = structlog.get_logger(__name__)

MOTIVO_DO_BLOQUEIO = (
    "o trabalhador como processo separado exige um repositorio de analise compartilhado, e ele "
    "nao existe neste projeto: a unica implementacao de RepositorioAnalises e em memoria, e o "
    "schema do Postgres tem apenas a tabela do RAG.\n\n"
    "Cada processo veria o proprio repositorio, e toda extracao falharia com 'documento nao esta "
    "na analise' — erro permanente, portanto sem recuperacao.\n\n"
    "Alternativas hoje:\n"
    "  CREDIT_TRABALHADOR_EM_PROCESSO=true   trabalhador junto da API (uma replica)\n"
    "  implementar RepositorioAnalisesPostgres   e o proximo passo do projeto\n"
)


class RepositorioCompartilhadoAusente(RuntimeError):
    """Levantada na subida. Tipo proprio para o teste afirmar o motivo, e nao a mensagem."""


def montar_e_rodar(settings: Settings) -> None:
    """Recusa subir, com o motivo.

    A verificacao vem **antes** de montar S3 e SQS de proposito: montar clientes para depois
    recusar daria a impressao, no log, de que a configuracao de armazenamento e o problema.
    """
    raise RepositorioCompartilhadoAusente(MOTIVO_DO_BLOQUEIO)


def main() -> None:
    settings = get_settings()
    configurar_logging(nivel=settings.nivel_log, formato_json=settings.log_json)
    logger.error("trabalhador.bloqueado", motivo="repositorio compartilhado ausente")
    montar_e_rodar(settings)


if __name__ == "__main__":
    main()
