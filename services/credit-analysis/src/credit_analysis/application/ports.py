"""Ports: as interfaces que a aplicacao exige do mundo externo.

Este e o ponto do Dependency Inversion. Os casos de uso dependem destes
Protocols, nunca de Postgres, do Tesseract ou da Anthropic. Trocar SQLite por
pgvector, ou Tesseract por Claude Vision, e escrever outro adapter — nenhuma
linha de caso de uso muda.

Usamos `typing.Protocol` em vez de ABC: o adapter nao precisa herdar nada, so
ter os metodos certos. Isso evita acoplar a infraestrutura ao pacote de
aplicacao, o que seria a propria dependencia que estamos tentando inverter.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable
from uuid import UUID

from credit_analysis.domain.documento import ImagemDocumento, ResultadoOCR
from credit_analysis.domain.entities import AnaliseCredito
from credit_analysis.domain.politica import TrechoPolitica, TrechoRecuperado


@runtime_checkable
class RepositorioAnalises(Protocol):
    """Persistencia do agregado AnaliseCredito."""

    async def salvar(self, analise: AnaliseCredito) -> None:
        """Grava ou atualiza a analise (upsert por id)."""
        ...

    async def buscar_por_id(self, analise_id: UUID) -> AnaliseCredito | None:
        """Devolve a analise ou None se nao existir."""
        ...

    async def listar(self, limite: int = 50, offset: int = 0) -> list[AnaliseCredito]:
        """Lista analises da mais recente para a mais antiga."""
        ...

    async def contar(self) -> int:
        """Total de analises armazenadas."""
        ...


@runtime_checkable
class ConsultaBureau(Protocol):
    """Consulta a bureau de credito (Serasa, SPC, ...).

    Abstraido desde a Camada 1 porque em producao e uma chamada de rede lenta
    e sujeita a falha: precisa de timeout, retry e circuit breaker no adapter,
    sem que o caso de uso saiba disso.
    """

    async def tem_restricao(self, cpf: str) -> bool:
        """True se houver restricao cadastral ativa."""
        ...


@runtime_checkable
class Embedder(Protocol):
    """Converte texto em vetor denso.

    Sincrono de proposito: e CPU-bound (inferencia ONNX local), nao I/O.
    Marcar como async daria a falsa impressao de que o event loop fica livre
    durante a chamada — nao fica. Quem precisar de nao-bloqueio usa um
    executor, e essa decisao fica visivel em quem chama.
    """

    @property
    def dimensoes(self) -> int:
        """Tamanho do vetor. Precisa casar com a coluna do vector store."""
        ...

    def vetorizar(self, textos: Sequence[str]) -> list[list[float]]:
        """Vetoriza documentos para indexacao."""
        ...

    def vetorizar_consulta(self, texto: str) -> list[float]:
        """Vetoriza uma pergunta.

        Separado de `vetorizar` porque alguns modelos (familia E5, BGE) exigem
        prefixos distintos para consulta e documento. Ignorar isso derruba a
        qualidade da busca de forma silenciosa.
        """
        ...


@runtime_checkable
class RepositorioPoliticas(Protocol):
    """Indice de trechos de politica — o vector store."""

    async def indexar(
        self, trechos: Sequence[TrechoPolitica], vetores: Sequence[Sequence[float]]
    ) -> None:
        """Grava trechos e seus vetores. Idempotente por `trecho.id`."""
        ...

    async def buscar_denso(
        self,
        vetor: Sequence[float],
        k: int = 5,
        produto: str | None = None,
    ) -> list[TrechoRecuperado]:
        """Busca por similaridade de cosseno, com filtro opcional por produto."""
        ...

    async def listar_todos(self) -> list[TrechoPolitica]:
        """Todos os trechos indexados — usado para montar o indice lexical."""
        ...

    async def contar(self) -> int: ...


@runtime_checkable
class MotorOCR(Protocol):
    """Extracao de texto de imagem.

    A confianca no retorno e o que torna este port util: sem ela nao ha como
    decidir entre aceitar o texto, escalar para um motor melhor ou mandar para
    revisao humana — e a POL-002 secao 3.2 exige exatamente essa decisao.
    """

    async def extrair(self, imagem: ImagemDocumento) -> ResultadoOCR:
        """Extrai texto e reporta a confianca media."""
        ...

    @property
    def identificacao(self) -> str:
        """Motor em uso, para registro de procedencia no parecer."""
        ...

    @property
    def custo_relativo(self) -> int:
        """Custo aproximado, para ordenar a cadeia de escalonamento.

        Escala arbitraria e comparativa (1 = local e gratuito). Existe para que
        a politica de escalonamento nao precise conhecer os motores concretos.
        """
        ...


@runtime_checkable
class ModeloLinguagem(Protocol):
    """Geracao de texto por LLM.

    A interface e minima de proposito: `sistema` + `usuario` -> texto. Manter
    o port pequeno e o que permite ter um fake deterministico util nos testes.
    Streaming e tool use, quando entrarem, viram ports separados em vez de
    inchar este.
    """

    async def gerar(self, sistema: str, usuario: str, max_tokens: int = 2048) -> str: ...

    @property
    def identificacao(self) -> str:
        """Modelo em uso, para registro no parecer e rastreabilidade."""
        ...


@runtime_checkable
class RelogioDominio(Protocol):
    """Fonte de tempo injetavel.

    Codigo que chama datetime.now() direto e impossivel de testar de forma
    deterministica. Injetar o relogio deixa os testes controlarem o tempo.
    """

    def agora(self) -> object: ...
