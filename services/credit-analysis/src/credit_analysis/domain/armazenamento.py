"""Referencia a um objeto guardado, e o estado do processamento assincrono.

## Por que a referencia carrega versao

`Referencia` e `(chave, versao)`, nao apenas a chave. A versao vem do bucket versionado e e o
que torna a referencia **imutavel**: a mesma chave pode receber outro conteudo, a mesma versao
nao.

Isso resolve tres problemas de uma vez:

1. **Idempotencia.** Entrega de evento de S3 e *at-least-once*: o mesmo upload pode disparar o
   processamento duas vezes. A versao e a chave natural de deduplicacao — sem ela, o dedupe
   seria por chave, e um reenvio legitimo do mesmo documento seria descartado como duplicata.
2. **Auditoria.** A POL-006 secao 5 exige guardar o original por 5 anos. "O documento que
   embasou este parecer" precisa apontar para um conteudo especifico, nao para o que estiver na
   chave hoje.
3. **Corrida entre reenvios.** Dois uploads simultaneos para a mesma analise produzem duas
   versoes, e cada extracao sabe qual conteudo leu. Com so a chave, a segunda extracao poderia
   aplicar o resultado da primeira.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


@dataclass(frozen=True, slots=True)
class Referencia:
    """Endereco imutavel de um objeto no armazenamento."""

    chave: str
    versao: str

    def __str__(self) -> str:
        # Formato estavel: vai para log e para mensagem de fila, e um `repr` de dataclass
        # mudaria de forma se um campo fosse adicionado.
        return f"{self.chave}@{self.versao}"


class EstadoDocumento(StrEnum):
    """Ciclo de vida de um documento no fluxo assincrono.

    Os estados existem porque o cliente **precisa distinguir** "ainda processando" de "falhou"
    de "processou e foi rejeitado por qualidade". Um `processado: bool` — que era o que havia
    quando o fluxo era sincrono — colapsa os tres em "false", e o canal de atendimento nao tem
    o que dizer a quem enviou o documento.
    """

    RECEBIDO = "recebido"
    """Guardado no armazenamento, na fila. O 202 devolve este estado."""

    EXTRAINDO = "extraindo"
    """Um trabalhador pegou a mensagem. Existe para separar "fila longa" de "trabalhador
    travado" — sem ele, os dois aparecem como `recebido` por muito tempo."""

    EXTRAIDO = "extraido"
    """OCR concluido e dados aplicados a analise."""

    REJEITADO = "rejeitado"
    """OCR concluido e **reprovado** no piso de qualidade da POL-002. Nao e falha do sistema: a
    acao do cliente e reenviar com mais resolucao, e a mensagem diz isso."""

    FALHOU = "falhou"
    """Erro de processamento apos as tentativas. Vai para revisao humana, e o motivo fica
    registrado — diferente de `rejeitado`, aqui a acao e de quem opera, nao do cliente."""

    @property
    def terminal(self) -> bool:
        """Se nao ha mais transicao esperada.

        Usado pelo endpoint de consulta para decidir se ainda vale o cliente voltar a perguntar.
        """
        return self in (EstadoDocumento.EXTRAIDO, EstadoDocumento.REJEITADO, EstadoDocumento.FALHOU)
