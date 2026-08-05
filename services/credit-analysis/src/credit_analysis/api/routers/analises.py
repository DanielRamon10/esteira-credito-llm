"""Rotas de analise de credito."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Query, Response, status

from credit_analysis.api.deps import (
    AnalisarDep,
    ConsultarDep,
    IdempotenciaDep,
    ListarDep,
    RepositorioDep,
)
from credit_analysis.api.observabilidade import registrar_parecer
from credit_analysis.api.schemas import (
    AnaliseRequest,
    AnaliseResponse,
    ErroResponse,
    PaginaAnalises,
)
from credit_analysis.api.seguranca import (
    ANALISES_ESCREVER,
    ANALISES_LER,
    Escopo,
    IdentidadeDep,
)
from credit_analysis.application.ports import RepositorioAnalises
from credit_analysis.application.use_cases.analisar_credito import ComandoAnalisar
from credit_analysis.domain.exceptions import (
    AnaliseNaoEncontrada,
    ChaveDeIdempotenciaAusente,
    ChaveDeIdempotenciaReusada,
    PedidoEmAndamento,
)
from credit_analysis.domain.idempotencia import (
    TAMANHO_MAXIMO_DA_CHAVE,
    EstadoDaChave,
    RegistroDeIdempotencia,
    impressao_do_pedido,
)
from credit_analysis.domain.value_objects import Dinheiro

router = APIRouter(prefix="/analises", tags=["Analises de credito"])

# Escopo declarado **por rota**, e nao um `dependencies=[...]` no router inteiro.
#
# Um escopo unico no router forcaria `analises:ler` e `analises:escrever` a serem o mesmo, e
# a granularidade e justamente o ponto: o canal que cria proposta nao precisa poder ler
# proposta alheia, e o painel de analista que le nao precisa poder criar.


@router.post(
    "",
    response_model=AnaliseResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submeter uma analise de credito",
    dependencies=[Depends(Escopo(ANALISES_ESCREVER))],
    responses={
        401: {"model": ErroResponse, "description": "Credencial ausente ou invalida"},
        403: {"model": ErroResponse, "description": "Sem o escopo analises:escrever"},
        422: {"model": ErroResponse, "description": "Payload invalido"},
        500: {"model": ErroResponse, "description": "Erro interno"},
    },
)
async def criar_analise(
    payload: AnaliseRequest,
    caso: AnalisarDep,
    repositorio: RepositorioDep,
    idempotencia: IdempotenciaDep,
    identidade: IdentidadeDep,
    resposta: Response,
    chave: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            max_length=TAMANHO_MAXIMO_DA_CHAVE,
            description=(
                "Chave unica do pedido, gerada pelo cliente (UUID e a escolha usual). "
                "Repetir a chave devolve a analise ja criada em vez de criar outra."
            ),
        ),
    ] = None,
) -> AnaliseResponse:
    """Executa a esteira de analise e devolve o parecer.

    ## A chave e obrigatoria, e isso e uma mudanca de contrato

    Sem ela, clique duplo, retry de cliente HTTP e reenvio depois de timeout criavam uma analise
    cada — com uma consulta a bureau cada. Em credito isso nao e desperdicio: consulta duplicada
    aparece no historico do proprio cliente.

    Exigir e nao apenas aceitar porque quem gera a chave e o cliente, e um cliente que nao a envia
    nao esta protegido. E a segunda mudanca de contrato deste projeto pela mesma razao da primeira
    (201 -> 202 na Camada 8): a alternativa correta era incompativel com a antiga.

    ## Tres desfechos

    - **chave nova** — processa, amarra a chave ao recurso e devolve 201;
    - **chave repetida, pedido igual** — le a analise criada e devolve 200. Nao 201: nada foi criado
      agora, e o cliente que insiste no 201 nao teria como distinguir os dois casos;
    - **chave repetida, pedido diferente** — 422. Devolver a resposta do primeiro faria o cliente
      concluir que submeteu uma analise que nao existe.

    ## A repeticao le o recurso, e nao um retrato dele

    `RegistroDeIdempotencia` guarda o **id**, nao o corpo — o motivo esta no cabecalho de
    `domain/idempotencia.py`, e ele e a Camada 10: guardar o corpo criaria uma segunda copia de dado
    pessoal fora do alcance de `apagar_identificacao`.

    Consequencia visivel: se a analise foi apagada a pedido do titular, a repeticao responde 404 em
    vez de ressuscita-la.
    """
    if chave is None:
        raise ChaveDeIdempotenciaAusente(
            "Idempotency-Key e obrigatorio nesta rota. Gere um valor unico por pedido (UUID "
            "serve) e reuse **o mesmo** ao repetir a chamada: sem ele, um reenvio cria uma "
            "segunda analise e uma segunda consulta a bureau para o mesmo solicitante."
        )

    # O locatario do token escopa a chave. Sem isso, um cliente que adivinhasse a chave de outro
    # receberia o recurso do outro na repeticao — ver o teste `test_a_chave_e_por_locatario`.
    #
    # `sujeito` quando nao ha locatario: o `sub` do token identifica o cliente OAuth, que e o
    # escopo certo num emissor sem multi-locacao.
    dono = identidade.locatario or identidade.sujeito
    impressao = impressao_do_pedido(payload.model_dump(mode="json"))
    agora = datetime.now(UTC)

    reivindicacao = await idempotencia.reivindicar(dono, chave, impressao, agora)

    if not reivindicacao.reivindicada:
        # 200 e nao 201: nada foi criado nesta chamada.
        #
        # O `status_code=201` do decorador vale para toda resposta bem-sucedida da rota, entao a
        # repeticao precisa sobrescrever aqui. Sem isso, um cliente que conta criacoes pelo codigo
        # contaria duas — e a distincao entre "criei" e "ja existia" e justamente o que ele ganha
        # ao mandar a chave.
        resposta.status_code = status.HTTP_200_OK
        return await _repetir(reivindicacao.registro, impressao, repositorio)

    comando = ComandoAnalisar(
        solicitante=payload.solicitante.para_dominio(),
        proposta=payload.proposta.para_dominio(),
        renda_comprovada=(
            Dinheiro(payload.renda_comprovada) if payload.renda_comprovada is not None else None
        ),
        meses_historico_bancario=payload.meses_historico_bancario,
    )
    try:
        analise = await caso.executar(comando)
    except Exception:
        # Libera a chave antes de propagar. Sem isto, uma falha transitoria — bureau em timeout,
        # banco fora por um segundo — envenenaria a chave pelo prazo de abandono, e o retry do
        # cliente receberia 409 por dois minutos. A idempotencia transformaria erro recuperavel em
        # bloqueio.
        await idempotencia.liberar(dono, chave)
        raise

    await idempotencia.concluir(dono, chave, analise.id)
    registrar_parecer(analise)
    return AnaliseResponse.de_dominio(analise)


async def _repetir(
    registro: RegistroDeIdempotencia | None,
    impressao: str,
    repositorio: RepositorioAnalises,
) -> AnaliseResponse:
    """Responde a uma chave ja reivindicada."""
    if registro is None:
        # A chave existia ao tentar reivindicar e sumiu na leitura seguinte — corrida com a purga.
        # Pedir para repetir e o desfecho seguro; assumir que e nova criaria a segunda analise.
        raise PedidoEmAndamento("Pedido em processamento. Repita a chamada com a mesma chave.")

    if registro.impressao != impressao:
        raise ChaveDeIdempotenciaReusada(
            "Idempotency-Key ja usado para um pedido diferente. Gere uma chave nova para cada "
            "pedido distinto; reusar a mesma faria esta chamada receber a resposta da anterior."
        )

    if registro.estado is EstadoDaChave.EM_ANDAMENTO or registro.recurso_id is None:
        raise PedidoEmAndamento(
            "O pedido com esta chave esta sendo processado. Repita em alguns segundos."
        )

    analise = await repositorio.buscar_por_id(registro.recurso_id)
    if analise is None:
        # O recurso foi apagado — pedido de exclusao do titular, por exemplo. Responder 404 e a
        # verdade; guardar o corpo da resposta teria feito esta chamada ressuscitar dado excluido.
        raise AnaliseNaoEncontrada(
            f"A analise criada por esta chave ({registro.recurso_id}) nao existe mais."
        )

    return AnaliseResponse.de_dominio(analise)


@router.get(
    "/{analise_id}",
    response_model=AnaliseResponse,
    summary="Consultar uma analise",
    dependencies=[Depends(Escopo(ANALISES_LER))],
    responses={
        401: {"model": ErroResponse, "description": "Credencial ausente ou invalida"},
        403: {"model": ErroResponse, "description": "Sem o escopo analises:ler"},
        404: {"model": ErroResponse, "description": "Analise nao encontrada"},
    },
)
async def consultar_analise(
    caso: ConsultarDep,
    analise_id: Annotated[UUID, Path(description="Identificador da analise")],
) -> AnaliseResponse:
    return AnaliseResponse.de_dominio(await caso.executar(analise_id))


@router.get(
    "",
    response_model=PaginaAnalises,
    summary="Listar analises",
    dependencies=[Depends(Escopo(ANALISES_LER))],
)
async def listar_analises(
    caso: ListarDep,
    limite: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PaginaAnalises:
    itens, total = await caso.executar(limite=limite, offset=offset)
    return PaginaAnalises(
        itens=[AnaliseResponse.de_dominio(a) for a in itens],
        total=total,
        limite=limite,
        offset=offset,
    )
