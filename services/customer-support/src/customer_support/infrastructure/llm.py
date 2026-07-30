"""Adapters de LLM deste servico.

O cliente Ollama vem de `plataforma.llm`. O que fica aqui e o **fake**, porque ele
depende do formato de prompt deste servico — a mesma razao pela qual o `LLMFake` do
`credit-analysis` nao foi para a biblioteca compartilhada: um duplo que imita um
prompt especifico nao e generico, e move-lo criaria acoplamento invertido.
"""

from __future__ import annotations

import re

from plataforma.llm import LLMOllama

__all__ = ["LLMFake", "LLMOllama"]


# Primeiro paragrafo do primeiro artigo no prompt.
_PRIMEIRO_ARTIGO = re.compile(r"\[([^\]]+)\]\s*\n(.+?)(?=\n\[|\Z)", re.DOTALL)


class LLMFake:
    """Duplo deterministico.

    Sem resposta fixa, ele **copia** o primeiro paragrafo do artigo que esta no
    prompt. Copiar em vez de inventar e o que permite exercitar o guard de divulgacao
    de forma util: a resposta gerada tem o mesmo perfil de conteudo que uma resposta
    real, e passa no guard pelas mesmas razoes.
    """

    def __init__(self, resposta: str | None = None) -> None:
        self._resposta = resposta
        self.chamadas: list[tuple[str, str]] = []

    @property
    def identificacao(self) -> str:
        return "fake"

    async def gerar(self, sistema: str, usuario: str, max_tokens: int = 1024) -> str:
        self.chamadas.append((sistema, usuario))

        if self._resposta is not None:
            return self._resposta

        casado = _PRIMEIRO_ARTIGO.search(usuario)
        if casado is None:
            return "Nao encontrei essa informacao na nossa base de ajuda."

        corpo = " ".join(casado.group(2).split())
        return corpo[:400]
