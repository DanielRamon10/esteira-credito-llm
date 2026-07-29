"""Modelo de chat com roteiro fixo, para testar o grafo sem modelo de verdade.

Por que nao usar o Ollama nos testes. Um passo do agente custa ~12s em CPU, o
resultado varia entre execucoes e a suite deixaria de rodar em qualquer maquina
sem Ollama instalado. Pior: testar *o grafo* contra um modelo real mede as duas
coisas ao mesmo tempo, e quando falha nao se sabe qual das duas quebrou.

O que se testa aqui e a mecanica: o teto de passos corta, o argumento invalido
volta corrigivel, o retorno de ferramenta entra como `ToolMessage`, a trilha
registra o que aconteceu. Se o modelo real escolhe bem a ferramenta e outra
pergunta, respondida por medicao em `tests/eval` e no cabecalho de `grafo.py`.

Diferente do `LLMFake`, este duplo nao vive em `src`: ele nao serve como
degradacao de producao. Sem modelo com suporte a ferramenta, o endpoint do
agente responde 503 em vez de fingir que atendeu.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import BaseTool
from pydantic import Field


def decisao_com_ferramenta(
    nome: str, id_chamada: str | None = None, **argumentos: Any
) -> AIMessage:
    """Decisao do modelo de chamar uma ferramenta."""
    return AIMessage(
        content="",
        tool_calls=[
            {"name": nome, "args": argumentos, "id": id_chamada or f"chamada_{nome}"},
        ],
    )


def decisao_multipla(*chamadas: tuple[str, dict[str, Any]]) -> AIMessage:
    """Varias ferramentas pedidas numa unica rodada.

    Existe porque e assim que o teto de passos pode ser burlado sem querer: se a
    contagem fosse por rodada em vez de por execucao, um modelo pedindo tres
    ferramentas de uma vez gastaria tres e contaria um.
    """
    return AIMessage(
        content="",
        tool_calls=[
            {"name": nome, "args": args, "id": f"chamada_{i}_{nome}"}
            for i, (nome, args) in enumerate(chamadas)
        ],
    )


def resposta_final(texto: str) -> AIMessage:
    """Resposta textual, sem ferramenta — encerra o grafo."""
    return AIMessage(content=texto)


class ChatFalso(BaseChatModel):
    """Devolve as mensagens programadas, em ordem, e registra o que recebeu."""

    respostas: list[BaseMessage] = Field(default_factory=list)
    ferramentas_vinculadas: list[str] = Field(default_factory=list)
    prompts_recebidos: list[list[BaseMessage]] = Field(default_factory=list)

    # O que estava vinculado em **cada** chamada, na ordem. E o registro que
    # permite afirmar que a ultima decisao rodou sem ferramenta nenhuma.
    ferramentas_por_chamada: list[list[str]] = Field(default_factory=list)

    # Atraso por chamada, consumido em paralelo com `respostas`. Existe para
    # testar o orcamento de tempo do agente com uma decisao rapida antes da
    # lenta — o que prova que a trilha parcial sobrevive ao cancelamento, e nao
    # apenas que o timeout dispara.
    atrasos: list[float] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "chat-falso"

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> BaseChatModel:
        """Devolve uma **copia rasa** com as ferramentas vinculadas.

        A escolha entre copia e `self` decide o que o teste consegue provar.

        Devolver `self` mutado seria mais simples e cegaria o teste mais
        importante daqui: o de que o no `concluir` chama o modelo **sem**
        ferramenta vinculada. Com uma unica instancia mutada, a lista de
        ferramentas fica a mesma nas duas chamadas e a diferenca desaparece.

        Copia rasa da o melhor dos dois lados. `ferramentas_vinculadas` passa a
        ser por instancia — a copia tem, a original nao —, enquanto `respostas`,
        `atrasos`, `prompts_recebidos` e `ferramentas_por_chamada` continuam
        sendo o **mesmo objeto de lista** nas duas. Assim a fila de respostas e
        consumida em ordem independente de quem invoca, e o historico completo
        continua visivel pelo objeto que o teste tem em maos.
        """
        return self.model_copy(
            update={"ferramentas_vinculadas": [_nome_da_ferramenta(t) for t in tools]}
        )

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.prompts_recebidos.append(list(messages))
        self.ferramentas_por_chamada.append(list(self.ferramentas_vinculadas))

        if self.atrasos:
            # `time.sleep` e nao `asyncio.sleep` porque o `_agenerate` padrao do
            # BaseChatModel roda este metodo num executor: aqui dentro nao ha
            # event loop para ceder.
            time.sleep(self.atrasos.pop(0))

        if not self.respostas:
            # Falha alta e explicita: o grafo pediu mais uma decisao do que o
            # teste previu. Devolver uma resposta vazia esconderia justamente o
            # bug de loop que estes testes existem para pegar.
            raise AssertionError(
                "ChatFalso ficou sem resposta programada — o grafo executou mais "
                f"decisoes do que o roteiro previa ({len(self.prompts_recebidos)} ate agora)."
            )

        return ChatResult(generations=[ChatGeneration(message=self.respostas.pop(0))])


def _nome_da_ferramenta(ferramenta: Any) -> str:
    if isinstance(ferramenta, dict):
        funcao = ferramenta.get("function", {})
        nome = funcao.get("name") if isinstance(funcao, dict) else None
        return str(nome or ferramenta.get("name", "?"))
    return str(getattr(ferramenta, "name", None) or getattr(ferramenta, "__name__", "?"))
