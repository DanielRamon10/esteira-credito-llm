"""Rota de upload e processamento de documento."""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, File, Form, Response, UploadFile, status
from fastapi import Path as PathParam

from credit_analysis.api.deps import (
    ReceberDocumentoDep,
    RepositorioDep,
    SettingsDep,
)
from credit_analysis.api.observabilidade import registrar_recepcao
from credit_analysis.api.schemas import (
    DocumentoAceitoResponse,
    DocumentoEstadoResponse,
    ErroResponse,
)
from credit_analysis.api.seguranca import ANALISES_LER, DOCUMENTOS_ENVIAR, Escopo
from credit_analysis.application.ports import BuscaPorDocumento, RepositorioAnalises
from credit_analysis.application.use_cases.extracao_assincrona import (
    ComandoReceberDocumento,
)
from credit_analysis.domain.entities import AnaliseCredito, DocumentoSubmetido
from credit_analysis.domain.enums import TipoDocumento
from credit_analysis.domain.exceptions import AnaliseNaoEncontrada, ValorInvalido
from credit_analysis.infrastructure.ocr.documentos import (
    EXTENSOES_IMAGEM,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/analises", tags=["Documentos"])

# Segundo router, com prefixo proprio.
#
# A recepcao vive sob `/analises/{id}/documentos` porque um documento **pertence** a uma analise.
# A consulta vive sob `/documentos/{id}` porque o cliente que recebeu 202 tem um identificador so,
# e obriga-lo a guardar os dois para acompanhar um upload seria mais RESTful e menos usavel.
consulta = APIRouter(prefix="/documentos", tags=["Documentos"])


class ArquivoGrandeDemais(ValorInvalido):
    """Upload acima do teto configurado."""

    codigo = "arquivo_grande_demais"


# Teto de tamanho. Extrato anual escaneado passa de 20MB; 32MB da folga sem
# permitir que um upload unico consuma memoria de forma desproporcional.
TAMANHO_MAXIMO_BYTES = 32 * 1024 * 1024

# Bloco de leitura no streaming para disco. Ler `await arquivo.read()` inteiro
# carregaria os 32MB em memoria por requisicao concorrente.
BLOCO = 1024 * 1024

EXTENSOES_ACEITAS = frozenset({".pdf"}) | EXTENSOES_IMAGEM

_RESPOSTAS: dict[int | str, dict[str, Any]] = {
    404: {"model": ErroResponse, "description": "Analise nao encontrada"},
    409: {"model": ErroResponse, "description": "Analise ja processada"},
    413: {"model": ErroResponse, "description": "Arquivo grande demais"},
    422: {"model": ErroResponse, "description": "Arquivo invalido ou ilegivel"},
}


@router.post(
    "/{analise_id}/documentos",
    response_model=DocumentoAceitoResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Enviar documento para extracao assincrona",
    dependencies=[Depends(Escopo(DOCUMENTOS_ENVIAR))],
    responses=_RESPOSTAS,
)
async def enviar_documento(
    caso: ReceberDocumentoDep,
    resposta: Response,
    settings: SettingsDep,
    analise_id: Annotated[UUID, PathParam(description="Identificador da analise")],
    tipo: Annotated[TipoDocumento, Form(description="Tipo do documento enviado")],
    arquivo: Annotated[UploadFile, File(description="PDF ou imagem do documento")],
) -> DocumentoAceitoResponse:
    """Guarda o documento e enfileira a extracao. Devolve **202**, nao 201.

    ## Por que 202

    A extracao roda OCR com escalonamento — segundos, e possivelmente uma chamada a modelo de
    visao. Mantendo isso na requisicao, o cliente espera com a conexao aberta, algum gateway
    decide o timeout, e nao ha como retentar sem ele reenviar o arquivo. O docstring da versao
    sincrona ja previa esta troca.

    ## O que o cliente perde, e onde ele recupera

    O piso de qualidade da POL-002 **deixou de recusar a requisicao**. Antes, confianca abaixo de
    60% devolvia 422 com a instrucao de reenviar; agora o 202 ja foi dado quando a extracao
    acontece.

    A instrucao continua integral, no campo `erro` de `GET /v1/documentos/{id}`, com o estado
    `rejeitado`. E pior para quem integra — chega depois e exige consultar — e foi aceito porque
    a alternativa e OCR de segundos dentro da requisicao.

    O que **nao** e aceitavel e a rejeicao virar silencio, e nao vira: o estado e terminal e
    carrega o motivo.
    """
    sufixo = Path(arquivo.filename or "").suffix.lower()
    if sufixo not in EXTENSOES_ACEITAS:
        raise ValorInvalido(
            f"Extensao '{sufixo or '(ausente)'}' nao aceita. Envie {sorted(EXTENSOES_ACEITAS)}"
        )

    # Streaming para temporario, e nao `await arquivo.read()` em memoria.
    #
    # O teto e 32MB: com N uploads concorrentes, ler inteiro segura N x 32MB. O temporario mantem
    # o consumo limitado ao bloco, e o `guardar` recebe um stream — o adapter de S3 envia em
    # partes sem materializar o objeto.
    #
    # Nome gerado por nos, nunca o enviado pelo cliente: usa-lo como caminho abriria path
    # traversal ("../../etc/passwd").
    with tempfile.TemporaryDirectory(prefix="credit-doc-") as pasta:
        destino = Path(pasta) / f"upload{sufixo}"
        tamanho, digest = await _gravar_com_limite(arquivo, destino)

        # O arquivo e reaberto para leitura e passado como stream. O hash veio do mesmo passo de
        # gravacao: calcula-lo aqui exigiria uma segunda passada sobre os mesmos bytes.
        aceito = await asyncio.to_thread(destino.open, "rb")
        try:
            resultado = await caso.executar(
                ComandoReceberDocumento(
                    analise_id=analise_id,
                    tipo=tipo,
                    nome_arquivo=arquivo.filename or f"documento{sufixo}",
                    conteudo=aceito,
                    conteudo_hash=digest,
                    tamanho_bytes=tamanho,
                    tipo_mime=arquivo.content_type or "application/octet-stream",
                    request_id=structlog.contextvars.get_contextvars().get("request_id", ""),
                )
            )
        finally:
            await asyncio.to_thread(aceito.close)

    consultar_em = f"{settings.prefixo_api}/documentos/{resultado.documento_id}"

    # `Location` como manda a RFC 7231 secao 6.3.2 para 202. Cliente HTTP generico o segue; o
    # mesmo caminho vai no corpo porque cliente escrito a mao raramente le cabecalho de resposta,
    # e um 202 cujo acompanhamento esta so no cabecalho e um 202 tratado como "deu certo, fim".
    resposta.headers["Location"] = consultar_em

    registrar_recepcao(resultado)
    return DocumentoAceitoResponse(
        documento_id=resultado.documento_id,
        analise_id=resultado.analise_id,
        estado=resultado.estado,
        consultar_em=consultar_em,
    )


@consulta.get(
    "/{documento_id}",
    response_model=DocumentoEstadoResponse,
    summary="Acompanhar a extracao de um documento",
    dependencies=[Depends(Escopo(ANALISES_LER))],
    responses={404: {"model": ErroResponse, "description": "Documento nao encontrado"}},
)
async def consultar_documento(
    repositorio: RepositorioDep,
    documento_id: Annotated[UUID, PathParam(description="Identificador do documento")],
) -> DocumentoEstadoResponse:
    """Estado do processamento, para o cliente que recebeu 202.

    ## Por que a rota nao esta sob `/analises/{id}/documentos/{id}`

    Seria mais RESTful e obrigaria o cliente a guardar **dois** identificadores para acompanhar
    um upload. O `Location` que o 202 devolve seria mais longo sem ganho: o `documento_id` e um
    UUID e ja identifica sozinho.

    O custo e uma busca por documento em vez de acesso direto. No Postgres e um `JOIN` pelo
    indice `idx_documento_id`; no repositorio em memoria e uma varredura, e o `_localizar` explica
    por que a escolha entre os dois e por capacidade do adapter e nao por configuracao.

    ## O escopo e `analises:ler`, e nao `documentos:enviar`

    Consultar estado e leitura, e quem envia documento nao precisa poder ler. Reaproveitar o
    escopo de envio daria a um cliente que so faz upload a capacidade de sondar o resultado de
    documentos que ele nao enviou — o `documento_id` e um UUID, mas obscuridade nao e controle
    de acesso.
    """
    analise, documento = await _localizar(repositorio, documento_id)
    return DocumentoEstadoResponse.de_dominio(analise, documento)


# Teto da varredura, quando o repositorio nao oferece busca por documento.
#
# **Nao e uma pagina**, e um limite de tolerancia: passando disto, a rota de polling responderia
# 404 para um documento que existe — o cliente que recebeu 202 concluiria que o upload dele se
# perdeu. Por isso o `_varrer` loga quando bate no teto, em vez de devolver 404 em silencio.
TETO_DA_VARREDURA = 1000


async def _localizar(
    repositorio: RepositorioAnalises, documento_id: UUID
) -> tuple[AnaliseCredito, DocumentoSubmetido]:
    """Encontra o documento e a analise que o contem.

    O documento nao tem repositorio proprio: ele e parte do agregado `AnaliseCredito`, e um
    repositorio separado quebraria a fronteira do agregado — dois pontos de escrita para o mesmo
    dado, com a consistencia entre eles a cargo de quem chamar na ordem certa.

    Sobram dois caminhos para ir do documento a analise, e qual deles roda depende do adapter:

    - `BuscaPorDocumento`, um `JOIN` com indice em `documento(id)`. E o que o Postgres oferece;
    - varredura, para quem nao oferece. Correto no volume do repositorio em memoria e **errado**
      em producao, que e exatamente por que o Postgres implementa o primeiro.

    A escolha e por capacidade e nao por configuracao: nao existe variavel que possa deixar o
    Postgres na varredura por engano.
    """
    if isinstance(repositorio, BuscaPorDocumento):
        analise = await repositorio.buscar_por_documento(documento_id)
        if analise is None:
            raise AnaliseNaoEncontrada(f"Documento {documento_id} nao encontrado")
    else:
        analise = await _varrer(repositorio, documento_id)

    for documento in analise.documentos:
        if documento.id == documento_id:
            return analise, documento

    # Alcancavel de um jeito so: a analise foi encontrada pelo `JOIN` e o documento nao esta na
    # lista que o `_montar` carregou. Isso seria inconsistencia entre as tabelas `analise` e
    # `documento`, e nao "nao encontrado" — mas o cliente da rota de polling nao tem o que fazer
    # com a distincao, e o 404 e a resposta honesta.
    raise AnaliseNaoEncontrada(f"Documento {documento_id} nao encontrado")


async def _varrer(repositorio: RepositorioAnalises, documento_id: UUID) -> AnaliseCredito:
    """Varredura, para o repositorio que nao oferece busca por documento."""
    analises = await repositorio.listar(limite=TETO_DA_VARREDURA)

    for analise in analises:
        if any(d.id == documento_id for d in analise.documentos):
            return analise

    if len(analises) >= TETO_DA_VARREDURA:
        # O 404 que vem depois pode ser mentira: o documento pode estar na analise 1001. Sem este
        # log, o sintoma seria um cliente reclamando de upload perdido e um servidor sem registro
        # de nada errado.
        logger.warning(
            "documento.varredura_no_teto",
            documento_id=str(documento_id),
            teto=TETO_DA_VARREDURA,
            detalhe="404 pode ser falso; este repositorio nao oferece BuscaPorDocumento",
        )

    raise AnaliseNaoEncontrada(f"Documento {documento_id} nao encontrado")


async def _gravar_com_limite(arquivo: UploadFile, destino: Path) -> tuple[int, str]:
    """Grava o upload em disco em blocos, abortando se passar do teto. Devolve (bytes, sha256).

    O hash sai daqui e nao de um passo separado porque esta funcao ja passa por cada byte uma
    vez. Calcula-lo depois exigiria uma segunda leitura do arquivo inteiro — 32MB de I/O para
    obter algo que estava disponivel de graca.

    Checar `arquivo.size` antes nao basta: o header `Content-Length` e informado
    pelo cliente e pode mentir. O limite tem que ser aplicado sobre o que
    realmente chega.

    ## As escritas vao para uma thread, e nao para o event loop

    A versao anterior chamava `saida.write(bloco)` direto dentro da corrotina. O
    lint de seguranca do outro servico apontou isso (regra ASYNC), e o apontamento
    esta correto: escrita em disco e chamada bloqueante, e enquanto ela roda **o
    event loop inteiro para**. Num upload de 10MB isso significa segurar todas as
    outras requisicoes do processo durante a gravacao.

    O padrao aqui e o que importa mais que a magnitude: `async def` cria a
    expectativa de que a funcao cede controle, e I/O sincrono dentro dela quebra
    essa expectativa em silencio. `asyncio.to_thread` move a operacao para o pool
    de threads, e o loop segue atendendo.
    """
    total = 0
    digest = hashlib.sha256()
    saida = await asyncio.to_thread(destino.open, "wb")
    try:
        while bloco := await arquivo.read(BLOCO):
            total += len(bloco)
            if total > TAMANHO_MAXIMO_BYTES:
                raise ArquivoGrandeDemais(
                    f"Arquivo excede o limite de {TAMANHO_MAXIMO_BYTES // (1024 * 1024)}MB"
                )
            digest.update(bloco)
            await asyncio.to_thread(saida.write, bloco)
    except ArquivoGrandeDemais:
        # Fecha antes de remover: no Windows, apagar arquivo com handle aberto
        # levanta PermissionError, e o parcial ficaria em disco.
        await asyncio.to_thread(saida.close)
        await asyncio.to_thread(destino.unlink, True)
        raise
    finally:
        # Idempotente: fechar duas vezes nao levanta.
        await asyncio.to_thread(saida.close)

    return total, digest.hexdigest()

    if total == 0:
        raise ValorInvalido("Arquivo vazio")
