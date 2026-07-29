"""Adapters do port `ModeloLinguagem`.

`LLMAnthropic` usa `langchain-anthropic`. A escolha do LangChain aqui e
deliberada e limitada: ele fica **atras do port**, entao o resto do sistema
nao importa nada de `langchain_*`. Se o LangChain sair, muda um arquivo.

`LLMFake` devolve resposta deterministica montada a partir dos trechos
recuperados. Isso permite testar o caminho completo de fundamentacao —
incluindo a verificacao de citacoes — sem chave de API e sem custo.
"""

from __future__ import annotations

import json
import re
from functools import cached_property
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:  # pragma: no cover - apenas para tipagem
    from langchain_anthropic import ChatAnthropic

logger = structlog.get_logger(__name__)

# Claude Opus 5. Nao acrescente sufixo de data ao id — ele e completo assim.
MODELO_PADRAO = "claude-opus-5"

# Teto de tokens da resposta. No Opus 5 o thinking e contado dentro do
# max_tokens junto com o texto, entao um valor apertado corta a resposta no
# meio depois de o modelo ter "pensado". Fundamentacao de parecer e curta, mas
# a folga aqui e barata.
MAX_TOKENS_PADRAO = 4096


class LLMAnthropic:
    """Claude via langchain-anthropic."""

    def __init__(
        self,
        modelo: str = MODELO_PADRAO,
        temperatura_desabilitada: bool = True,
        timeout_segundos: float = 60.0,
    ) -> None:
        self._modelo = modelo
        self._timeout = timeout_segundos
        # Opus 5 rejeita `temperature`/`top_p`/`top_k` com 400. O flag existe
        # so para documentar que a ausencia e intencional, nao esquecimento.
        self._temperatura_desabilitada = temperatura_desabilitada

    @cached_property
    def _cliente(self) -> ChatAnthropic:
        # Import tardio: `langchain_anthropic` puxa o SDK inteiro e nada disso
        # e necessario quando o servico roda com o LLM fake.
        from langchain_anthropic import ChatAnthropic

        # Nomes canonicos dos campos (`model_name`, `max_tokens_to_sample` e
        # `timeout` sao apenas aliases; usar o alias passa em runtime mas o
        # type checker reclama, e o alias pode sumir numa versao futura).
        return ChatAnthropic(
            model=self._modelo,
            max_tokens=MAX_TOKENS_PADRAO,
            default_request_timeout=self._timeout,
            max_retries=2,
        )

    @property
    def identificacao(self) -> str:
        return self._modelo

    async def gerar(self, sistema: str, usuario: str, max_tokens: int = MAX_TOKENS_PADRAO) -> str:
        resposta = await self._cliente.ainvoke(
            [("system", sistema), ("human", usuario)],
            max_tokens=max_tokens,
        )

        texto = resposta.text if isinstance(resposta.text, str) else str(resposta.content)

        logger.info(
            "llm.resposta",
            modelo=self._modelo,
            tokens_entrada=resposta.usage_metadata.get("input_tokens")
            if resposta.usage_metadata
            else None,
            tokens_saida=resposta.usage_metadata.get("output_tokens")
            if resposta.usage_metadata
            else None,
        )
        return str(texto)


class LLMFake:
    """LLM deterministico para testes e para rodar o servico sem chave.

    Nao tenta imitar linguagem natural: monta um JSON valido citando os
    primeiros trechos que aparecem no prompt. O objetivo e exercitar o
    contrato (formato de saida, verificacao de citacao), nao a redacao.
    """

    def __init__(self, resposta_fixa: str | None = None) -> None:
        self._resposta_fixa = resposta_fixa
        self.chamadas: list[tuple[str, str]] = []

    @property
    def identificacao(self) -> str:
        return "fake"

    async def gerar(self, sistema: str, usuario: str, max_tokens: int = MAX_TOKENS_PADRAO) -> str:
        self.chamadas.append((sistema, usuario))

        if self._resposta_fixa is not None:
            return self._resposta_fixa

        return json.dumps(
            {
                "fundamentacao": "Analise fundamentada nas politicas recuperadas.",
                "citacoes": _citacoes_do_prompt(usuario),
            },
            ensure_ascii=False,
        )


# Casa os blocos de trecho que o prompt monta:  [POL-001 v3.2 | 2. Faixas]
_BLOCO = re.compile(r"\[([A-Z]+-\d+) v([\d.]+) \| ([^\]]+)\]\n(.*?)(?=\n\[|\Z)", re.DOTALL)


def _citacoes_do_prompt(prompt: str) -> list[dict[str, str]]:
    """Extrai citacoes validas dos dois primeiros trechos presentes no prompt."""
    citacoes: list[dict[str, str]] = []

    for politica, versao, secao, corpo in _BLOCO.findall(prompt)[:2]:
        primeira_linha = next((linha.strip() for linha in corpo.splitlines() if linha.strip()), "")
        citacoes.append(
            {
                "politica": politica,
                "versao": versao,
                "secao": secao.strip(),
                "trecho": primeira_linha,
            }
        )

    return citacoes
