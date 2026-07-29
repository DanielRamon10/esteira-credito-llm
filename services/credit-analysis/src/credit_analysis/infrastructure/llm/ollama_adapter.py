"""Adapter Ollama do port `ModeloLinguagem`.

Modelo rodando na propria maquina: sem chave, sem conta, sem limite de
requisicao e sem custo por token. Para uma esteira de credito ha um argumento
que pesa mais que o preco — **o dado nao sai da infraestrutura**. Prompt de
fundamentacao carrega renda, CPF mascarado e trecho de politica interna;
manda-los para uma API de terceiro exige avaliacao de LGPD e contrato, nao
apenas uma chave. E a mesma razao pela qual os embeddings ja rodam local.

O preco disso e latencia: em CPU, um modelo 8B responde em torno de 70s. Isso
e aceitavel numa esteira assincrona e inaceitavel num endpoint sincrono — a
Camada 4 (agent) e a 5 (observabilidade) e que vao expor esse custo de verdade.

## Escolha do modelo, por medicao

Medido nesta maquina (Core Ultra 7 165U, 12 nucleos, sem GPU) contra o
**guardrail real de citacoes** — nao contra uma metrica generica de qualidade:

    modelo          seg   tok/s  alegadas  confirmadas  rejeitadas
    llama3.2:3b    49,3     7,4         3            2           1
    llama3.1:8b    74,2     4,5         2            2           0  <- padrao
    qwen2.5:7b     96,3     5,7         2            2           0

Duas conclusoes que so aparecem medindo:

1. **Maior nem sempre e melhor nesta tarefa.** Antes de uma correcao na
   verificacao, o 3B batia os dois maiores. A tarefa aqui e *copiar texto
   literalmente*, e modelo grande tende a "melhorar" o que copia — parafrasear,
   normalizar, resumir. Exatamente o que o guardrail rejeita.
2. **`format="json"` do Ollama e ~25% mais rapido**, alem de garantir o
   formato. Restringir a gramatica de saida reduz o espaco de busca.

`llama3.1:8b` e o padrao por ser o mais rapido entre os que chegam a zero
rejeicao. `llama3.2:3b` fica documentado como opcao de desenvolvimento: 33%
mais rapido, mas produz citacao rejeitada, o que derruba `Fundamentacao.confiavel`.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog

from credit_analysis.infrastructure.observabilidade import metricas
from credit_analysis.infrastructure.observabilidade.tracing import marcar_erro, span

if TYPE_CHECKING:  # pragma: no cover
    from langchain_ollama import ChatOllama

logger = structlog.get_logger(__name__)

MODELO_PADRAO = "llama3.1:8b"

# Alternativa rapida para desenvolvimento. Ver a medicao no cabecalho: e mais
# veloz, mas parafraseia, e parafrase e rejeitada pelo guardrail.
MODELO_RAPIDO = "llama3.2:3b"

ENDPOINT_PADRAO = "http://127.0.0.1:11434"

# Determinismo: a fundamentacao de um parecer de credito nao deve variar entre
# execucoes sobre a mesma entrada. Numa esteira auditavel, "por que este parecer
# saiu diferente do de ontem?" precisa ter resposta.
TEMPERATURA = 0.0

# Inferencia em CPU e lenta; o timeout precisa acomodar isso sem mascarar um
# Ollama travado.
TIMEOUT_PADRAO = 240.0

# Janela de contexto. **Precisa ser explicita.**
#
# O Ollama usa 2048 tokens por padrao, e um prompt maior e truncado **em
# silencio** — sem erro, sem aviso. O prompt de fundamentacao carrega 5 trechos
# de politica (ate 1800 caracteres cada) mais o caso e as regras do sistema, o
# que passa folgadamente de 2048. O sintoma seria o modelo ignorar os ultimos
# trechos recuperados e citar so os primeiros, parecendo um problema de
# retrieval ou de prompt quando na verdade o texto nunca chegou nele.
#
# 8192 cobre o prompt atual com margem. Custa RAM (o KV cache cresce com a
# janela), o que nesta maquina e barato.
NUM_CTX = 8192


class LLMOllama:
    """Modelo local via Ollama, atras do mesmo port do adapter Anthropic."""

    def __init__(
        self,
        modelo: str = MODELO_PADRAO,
        endpoint: str = ENDPOINT_PADRAO,
        timeout_segundos: float = TIMEOUT_PADRAO,
        forcar_json: bool = True,
    ) -> None:
        self._modelo = modelo
        self._endpoint = endpoint
        self._timeout = timeout_segundos
        self._forcar_json = forcar_json
        self._clientes: dict[int, ChatOllama] = {}

    def _obter_cliente(self, max_tokens: int) -> ChatOllama:
        """Cliente para um dado teto de saida, memoizado por valor.

        `num_predict` (o equivalente do `max_tokens`) so e aceito no construtor
        do `ChatOllama` — passa-lo em `ainvoke` ou via `bind()` levanta
        `TypeError: AsyncClient.chat() got an unexpected keyword argument`.
        Como o port permite `max_tokens` por chamada, guardamos um cliente por
        valor distinto. Construir e barato (nao abre conexao) e na pratica ha um
        ou dois valores em uso.
        """
        if (existente := self._clientes.get(max_tokens)) is not None:
            return existente

        # Import tardio: mantem o servico subindo mesmo sem langchain-ollama
        # instalado, desde que outro adapter esteja configurado.
        from langchain_ollama import ChatOllama

        extras: dict[str, Any] = {}
        if self._forcar_json:
            # Restringe a gramatica de saida a JSON valido. Elimina a classe de
            # falha em que o modelo devolve prosa antes ou depois do objeto — o
            # `_parsear_resposta` tolera, mas nao precisa. Medido: ~25% mais
            # rapido tambem, por reduzir o espaco de busca.
            extras["format"] = "json"

        cliente = ChatOllama(
            model=self._modelo,
            base_url=self._endpoint,
            temperature=TEMPERATURA,
            num_predict=max_tokens,
            num_ctx=NUM_CTX,
            client_kwargs={"timeout": self._timeout},
            **extras,
        )
        self._clientes[max_tokens] = cliente
        return cliente

    @property
    def identificacao(self) -> str:
        return f"ollama:{self._modelo}"

    async def gerar(self, sistema: str, usuario: str, max_tokens: int = 2048) -> str:
        inicio = time.perf_counter()

        try:
            with span(
                "llm.gerar",
                **{
                    "llm.modelo": self._modelo,
                    "llm.max_tokens": max_tokens,
                    # Tamanho do prompt, nao o prompt: ele carrega renda, CPF
                    # mascarado e trecho de politica interna.
                    "llm.tamanho_prompt": len(sistema) + len(usuario),
                },
            ):
                resposta = await self._obter_cliente(max_tokens).ainvoke(
                    [("system", sistema), ("human", usuario)]
                )
        except Exception as exc:
            # A metrica de falha e registrada aqui e a excecao segue subindo: quem
            # chamou decide o que fazer, mas a contagem nao pode depender disso.
            metricas.llm_chamadas.labels(
                modelo=self.identificacao, operacao="gerar", resultado="erro"
            ).inc()
            marcar_erro(exc)
            raise

        duracao = time.perf_counter() - inicio
        texto = resposta.text if isinstance(resposta.text, str) else str(resposta.content)
        uso = dict(resposta.usage_metadata or {})

        metricas.llm_duracao.labels(modelo=self.identificacao, operacao="gerar").observe(duracao)
        metricas.llm_chamadas.labels(
            modelo=self.identificacao, operacao="gerar", resultado="ok"
        ).inc()
        for direcao, chave in (("entrada", "input_tokens"), ("saida", "output_tokens")):
            # `usage_metadata` e um dict de tipo aberto: o provedor pode devolver
            # None, string ou nada. Contador que recebe lixo levanta excecao no
            # meio de uma resposta que ja foi gerada com sucesso.
            if isinstance(quantidade := uso.get(chave), int | float):
                metricas.llm_tokens.labels(modelo=self.identificacao, direcao=direcao).inc(
                    float(quantidade)
                )

        logger.info(
            "llm.resposta",
            modelo=self.identificacao,
            tokens_entrada=uso.get("input_tokens"),
            tokens_saida=uso.get("output_tokens"),
            caracteres=len(texto),
            duracao_ms=int(duracao * 1000),
        )
        return texto


def criar_chat_ollama(
    modelo: str = MODELO_PADRAO,
    endpoint: str = ENDPOINT_PADRAO,
    timeout_segundos: float = TIMEOUT_PADRAO,
) -> ChatOllama:
    """Cliente de chat cru, para quem precisa de `bind_tools`.

    O `LLMOllama` acima implementa o port `ModeloLinguagem` — sistema + usuario
    -> texto — e essa interface estreita e proposital. O agente da Camada 4
    precisa de outra coisa: vincular ferramentas, receber `tool_calls` de volta e
    devolver `ToolMessage`. Espremer isso no port de geracao de texto inflaria
    justamente o contrato que se manteve minimo para ter um fake util.

    Entao o agente recebe o `ChatOllama` diretamente. Ele e adapter de
    infraestrutura conversando com biblioteca de infraestrutura; a fronteira
    hexagonal que importa fica um nivel acima, no port `AgenteCredito`, que
    devolve `TrilhaAgente` — tipo de dominio, sem nada de LangChain.

    Sem `format="json"` aqui: forcar JSON quebra tool calling, porque a resposta
    com chamada de ferramenta tem estrutura propria no protocolo do Ollama.
    """
    from langchain_ollama import ChatOllama

    return ChatOllama(
        model=modelo,
        base_url=endpoint,
        temperature=TEMPERATURA,
        num_ctx=NUM_CTX,
        client_kwargs={"timeout": timeout_segundos},
    )


ESQUEMAS_PERMITIDOS = frozenset({"http", "https"})


def _validar_endpoint(endpoint: str) -> str:
    """Recusa esquema que nao seja HTTP.

    `urlopen` aceita `file:`, `ftp:` e esquemas customizados. Como o endpoint vem
    de variavel de ambiente, um valor como `file:///etc/passwd/api/version` faria a
    checagem de disponibilidade ler arquivo local — SSRF de configuracao.

    Aqui o risco e baixo (quem edita a variavel de ambiente ja tem acesso ao
    processo), mas o custo de fechar e uma funcao de tres linhas, e o padrao
    "urlopen sobre string configuravel" e o mesmo que vira vulnerabilidade quando o
    endpoint passa a vir de um cadastro no banco. Fechar agora evita que a proxima
    pessoa copie o padrao aberto.
    """
    from urllib.parse import urlsplit

    esquema = urlsplit(endpoint).scheme.lower()
    if esquema not in ESQUEMAS_PERMITIDOS:
        raise ValueError(
            f"Endpoint do Ollama com esquema nao permitido: {esquema or '(vazio)'}. "
            f"Use um de: {sorted(ESQUEMAS_PERMITIDOS)}"
        )
    return endpoint.rstrip("/")


def ollama_disponivel(endpoint: str = ENDPOINT_PADRAO, timeout: float = 2.0) -> bool:
    """Verifica se o daemon do Ollama responde.

    Usado no composition root para decidir entre o adapter local e o fake, em
    vez de deixar a aplicacao subir e falhar na primeira requisicao.
    """
    import urllib.error
    import urllib.request

    try:
        alvo = f"{_validar_endpoint(endpoint)}/api/version"
    except ValueError:
        logger.warning("llm.endpoint_invalido", endpoint=endpoint)
        return False

    try:
        # O esquema ja foi validado acima; `urlopen` nao pode mais abrir `file:`.
        with urllib.request.urlopen(alvo, timeout=timeout) as resposta:  # noqa: S310
            return bool(resposta.status == 200)
    except (urllib.error.URLError, OSError, ValueError):
        return False


def modelos_instalados(endpoint: str = ENDPOINT_PADRAO, timeout: float = 3.0) -> tuple[str, ...]:
    """Lista os modelos disponiveis no Ollama local.

    Permite avisar 'o modelo X nao esta baixado, rode ollama pull X' em vez de
    devolver um erro do daemon no meio de uma requisicao de negocio.
    """
    import json
    import urllib.error
    import urllib.request

    try:
        alvo = f"{_validar_endpoint(endpoint)}/api/tags"
    except ValueError:
        return ()

    try:
        # Esquema validado; ver `_validar_endpoint`.
        with urllib.request.urlopen(alvo, timeout=timeout) as resposta:  # noqa: S310
            dados = json.loads(resposta.read())
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return ()

    modelos = dados.get("models", []) if isinstance(dados, dict) else []
    return tuple(str(m.get("name", "")) for m in modelos if m.get("name"))
