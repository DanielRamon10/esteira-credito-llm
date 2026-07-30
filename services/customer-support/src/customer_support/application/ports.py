"""Ports do customer-support."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from customer_support.domain.conhecimento import Artigo, ArtigoRecuperado


@runtime_checkable
class BaseDeConhecimento(Protocol):
    """Busca nos artigos.

    `apenas_publicos` e parametro e nao default implicito de proposito: quem chama
    declara a intencao, e o unico ponto do codigo que passa `False` e o carregamento
    do proprio indice. Um default silencioso seria a forma mais facil de um artigo
    interno chegar ao cliente.
    """

    def buscar(
        self, pergunta: str, k: int = 3, apenas_publicos: bool = True
    ) -> list[ArtigoRecuperado]: ...

    def todos(self) -> list[Artigo]: ...

    @property
    def total(self) -> int: ...

    @property
    def procedencia(self) -> str: ...


@runtime_checkable
class ModeloLinguagem(Protocol):
    """Geracao de texto. Mesmo contrato minimo dos outros servicos."""

    async def gerar(self, sistema: str, usuario: str, max_tokens: int = 1024) -> str: ...

    @property
    def identificacao(self) -> str: ...
