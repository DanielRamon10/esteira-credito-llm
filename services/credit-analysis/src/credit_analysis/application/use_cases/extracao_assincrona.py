"""Recepcao e extracao de documento, separadas em duas metades.

## Por que o fluxo foi partido

Antes da Camada 8, `POST /v1/analises/{id}/documentos` fazia tudo dentro da requisicao: gravava
o arquivo num diretorio temporario, rodava OCR, aplicava o resultado e devolvia 201. O docstring
daquele endpoint ja previa a mudanca — "quando OCR e LLM entrarem, este endpoint vira 202
Accepted + polling".

O que forcou a mudanca nao foi elegancia: **OCR com escalonamento leva segundos e pode chamar
modelo de visao**, e manter isso numa requisicao HTTP significa um cliente esperando com uma
conexao aberta, um timeout de gateway a decidir, e nenhuma forma de retentar sem o cliente
reenviar o arquivo.

## A fronteira, e por que ela cai exatamente aqui

    ReceberDocumento    valida -> guarda -> registra `recebido` -> enfileira -> 202
    ExtrairDocumento    le bytes -> OCR                                   <- Lambda
    AplicarExtracao     envelope de injecao -> piso de qualidade -> anexa -> reavalia

`ExtrairDocumento` e a unica das tres que **nao toca no repositorio**. Ela precisa de duas
coisas: armazenamento e motor de OCR. E isso que a torna implantavel como funcao serverless — e
e por isso que a fronteira nao esta um passo antes nem um passo depois.

Se a extracao tambem anexasse o documento a analise, precisaria do repositorio, do bureau e do
motor de score; a funcao passaria a carregar o dominio de credito inteiro e deixaria de fazer
sentido como Lambda. Se ela apenas baixasse os bytes, a parte cara (OCR) continuaria na API.

## O que a assincronia custou, e nao e pouco

O piso de qualidade da POL-002 **deixou de poder recusar a requisicao**. Antes, confianca abaixo
de 60% levantava `DadosInsuficientes` e o cliente recebia 422 com a instrucao de reenviar. Agora
o cliente ja recebeu 202 quando a extracao acontece, e a rejeicao vira **estado** do documento:
`GET /v1/documentos/{id}` devolve `rejeitado` com a mesma instrucao.

Isso e pior para quem integra — a informacao chega depois, e exige consultar. Foi aceito porque a
alternativa e manter OCR de segundos numa requisicao sincrona, e porque a instrucao continua
chegando integralmente. O que **nao** seria aceitavel e a rejeicao virar silencio: o estado e
terminal e carrega o motivo.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import IO
from uuid import UUID

import structlog

from credit_analysis.application.ports import (
    ArmazenamentoDocumentos,
    FilaDeTrabalho,
    MotorOCR,
    RepositorioAnalises,
)
from credit_analysis.domain.armazenamento import EstadoDocumento, Referencia
from credit_analysis.domain.documento import ResultadoOCR
from credit_analysis.domain.entities import DocumentoSubmetido
from credit_analysis.domain.enums import StatusAnalise, TipoDocumento
from credit_analysis.domain.exceptions import (
    AnaliseNaoEncontrada,
    DadosInsuficientes,
    ValorInvalido,
)
from credit_analysis.domain.extracao_assincrona import PedidoDeExtracao
from credit_analysis.domain.value_objects import Percentual
from credit_analysis.infrastructure.ocr import documentos as leitor

logger = structlog.get_logger(__name__)

# Prefixo das chaves no armazenamento.
#
# A analise entra no caminho para que a politica de ciclo de vida do bucket e uma eventual
# exclusao por titular (LGPD art. 18) possam operar por prefixo. Sem ela, apagar os documentos
# de uma pessoa exigiria varrer o bucket inteiro.
PREFIXO_CHAVE = "documentos"


@dataclass(frozen=True, slots=True)
class ComandoReceberDocumento:
    """Entrada da recepcao.

    `conteudo` e um stream e `conteudo_hash` vem pronto, calculado por quem leu o upload. As
    duas coisas andam juntas: com stream, calcular o hash aqui exigiria ler tudo (perdendo o
    ganho) ou ler duas vezes (rebobinando um objeto que pode nao ser rebobinavel).

    Quem grava o upload ja passa por cada byte uma vez — e o lugar natural para o hash.
    """

    analise_id: UUID
    tipo: TipoDocumento
    nome_arquivo: str
    conteudo: IO[bytes]
    conteudo_hash: str
    tamanho_bytes: int
    tipo_mime: str
    request_id: str = ""


@dataclass(frozen=True, slots=True)
class DocumentoAceito:
    """O que o 202 devolve.

    Carrega `documento_id` e nao apenas "aceito": sem o identificador o cliente nao tem o que
    consultar, e um 202 sem forma de acompanhar e indistinguivel de perder o arquivo.
    """

    analise_id: UUID
    documento_id: UUID
    referencia: Referencia
    estado: EstadoDocumento


class ReceberDocumento:
    """Guarda o documento, registra como `recebido` e enfileira.

    ## A ordem das quatro operacoes nao e arbitraria

    guardar -> registrar -> salvar -> enfileirar

    Cada inversao tem uma consequencia:

    - **enfileirar antes de salvar** cria uma corrida real: o trabalhador pode consumir a
      mensagem e nao achar o documento na analise. Ele trataria como erro transitorio e
      retentaria, entao o sistema se recuperaria — mas o log ficaria com falhas fantasma que
      ninguem consegue explicar;
    - **enfileirar antes de guardar** e pior: a extracao falha porque o objeto nao existe, e
      esgotaria as tentativas antes de o upload terminar;
    - **guardar depois de registrar** deixaria um documento na analise apontando para uma
      referencia que nao existe, e nada no sistema o corrigiria.

    O caso que sobra e falha ao enfileirar com tudo ja gravado: o documento fica `recebido` para
    sempre. E o unico modo de falha em aberto aqui, e ele e **visivel** — o estado nao avanca, e
    o alerta de documentos parados o pega. A alternativa (transacao entre S3, banco e fila) exige
    outbox, que e desproporcional nesta escala e cujo custo esta anotado no README.
    """

    def __init__(
        self,
        repositorio: RepositorioAnalises,
        armazenamento: ArmazenamentoDocumentos,
        fila: FilaDeTrabalho,
    ) -> None:
        self._repositorio = repositorio
        self._armazenamento = armazenamento
        self._fila = fila

    async def executar(self, comando: ComandoReceberDocumento) -> DocumentoAceito:
        analise = await self._repositorio.buscar_por_id(comando.analise_id)
        if analise is None:
            raise AnaliseNaoEncontrada(f"Analise {comando.analise_id} nao encontrada")

        if comando.tamanho_bytes == 0:
            # Antes do armazenamento: gravar zero byte e enfileirar produziria uma falha de
            # extracao para algo que a borda podia recusar de imediato.
            raise ValorInvalido("Arquivo vazio")

        log = logger.bind(
            analise_id=str(comando.analise_id),
            tipo_documento=comando.tipo.value,
            arquivo=comando.nome_arquivo,
            bytes=comando.tamanho_bytes,
        )

        documento = DocumentoSubmetido(
            tipo=comando.tipo,
            nome_arquivo=comando.nome_arquivo,
            conteudo_hash=comando.conteudo_hash,
        )

        # A chave inclui o id do documento, nao apenas o da analise: dois documentos do mesmo
        # tipo na mesma analise (um reenvio depois de rejeicao, por exemplo) precisam coexistir.
        # Com chave por tipo, o segundo sobrescreveria o primeiro — e o versionamento guardaria
        # os dois, mas a referencia do primeiro deixaria de ser encontravel por listagem.
        chave = f"{PREFIXO_CHAVE}/{comando.analise_id}/{documento.id}/{comando.nome_arquivo}"
        referencia = await self._armazenamento.guardar(chave, comando.conteudo, comando.tipo_mime)
        documento.referencia = referencia

        # Analise ja avaliada precisa ser reaberta antes de receber documento: o parecer nao
        # pode ficar descolado da evidencia que o sustenta. A reabertura acontece **na
        # recepcao** e nao na aplicacao, para que o estado da analise reflita imediatamente que
        # ha trabalho pendente sobre ela.
        if analise.status is StatusAnalise.CONCLUIDA:
            analise.reabrir_para_reavaliacao(
                f"documento {comando.tipo.value} apresentado pelo solicitante"
            )
            log.info("analise.reaberta", reavaliacoes=analise.reavaliacoes)

        analise.anexar_documento(documento)
        await self._repositorio.salvar(analise)

        await self._fila.publicar(
            PedidoDeExtracao(
                analise_id=comando.analise_id,
                documento_id=documento.id,
                referencia=referencia,
                tipo=comando.tipo,
                nome_arquivo=comando.nome_arquivo,
                request_id=comando.request_id,
            )
        )

        log.info("documento.recebido", documento_id=str(documento.id), referencia=str(referencia))

        return DocumentoAceito(
            analise_id=comando.analise_id,
            documento_id=documento.id,
            referencia=referencia,
            estado=documento.estado,
        )


# ------------------------------------------------------- Texto: camada ou OCR


async def obter_texto(carregado: leitor.DocumentoCarregado, motor: MotorOCR) -> ResultadoOCR:
    """Usa a camada de texto do PDF quando existe; OCR so quando necessario."""
    if carregado.origem_sugerida is leitor.OrigemTexto.CAMADA_PDF:
        # Texto embutido e exato — nao ha reconhecimento envolvido, entao a
        # confianca e total. Rodar OCR aqui trocaria certeza por estimativa.
        return ResultadoOCR(
            texto=carregado.texto_embutido,
            confianca=Percentual.de(100),
            motor="pdf:camada_texto",
            palavras_reconhecidas=len(carregado.texto_embutido.split()),
            correcoes_aplicadas=("texto extraido da camada do PDF, sem OCR",),
        )

    # Multipagina: concatena o texto e usa a menor confianca das paginas. A
    # media esconderia uma pagina ilegivel no meio de um lote bom.
    textos: list[str] = []
    piores: list[Percentual] = []
    motores: set[str] = set()
    correcoes: set[str] = set()

    for pagina in carregado.paginas:
        if pagina.imagem is None:
            continue
        resultado = await motor.extrair(pagina.imagem)
        textos.append(resultado.texto)
        piores.append(resultado.confianca)
        motores.add(resultado.motor)
        correcoes.update(resultado.correcoes_aplicadas)

    if not textos:
        raise DadosInsuficientes("Documento sem pagina processavel")

    return ResultadoOCR(
        texto="\n\n".join(textos),
        confianca=min(piores),
        motor="+".join(sorted(motores)),
        palavras_reconhecidas=sum(len(t.split()) for t in textos),
        correcoes_aplicadas=tuple(sorted(correcoes)),
    )


@dataclass(frozen=True, slots=True)
class ResultadoExtracao:
    """Saida da metade que roda como Lambda."""

    ocr: ResultadoOCR
    paginas_ignoradas: int


class ExtrairDocumento:
    """Le os bytes e roda OCR. **Nao conhece analise de credito.**

    Duas dependencias, e a contagem e o ponto: armazenamento e motor de OCR. Sem repositorio,
    sem bureau, sem LLM, sem banco. E a razao pela qual esta classe cabe numa funcao serverless
    e as outras duas nao.

    Ela tambem nao decide nada sobre qualidade. O piso da POL-002 e regra de negocio e mora em
    `AplicarExtracao` — se estivesse aqui, a politica de credito passaria a viver numa funcao
    que roda fora do servico, e mudar o piso exigiria implantar a Lambda.
    """

    def __init__(self, armazenamento: ArmazenamentoDocumentos, motor_ocr: MotorOCR) -> None:
        self._armazenamento = armazenamento
        self._motor = motor_ocr

    async def executar(self, referencia: Referencia, nome_arquivo: str) -> ResultadoExtracao:
        conteudo = await self._armazenamento.obter(referencia)

        # `carregar_de_bytes` e nao `carregar(caminho)`: numa Lambda o disco e efemero e limitado,
        # e escrever o arquivo para depois le-lo seria I/O sem proposito. O leitor precisa do
        # nome apenas para escolher o decodificador pela extensao.
        carregado = leitor.carregar_de_bytes(conteudo, nome_arquivo)
        ocr = await obter_texto(carregado, self._motor)

        logger.info(
            "documento.extraido",
            referencia=str(referencia),
            motor=ocr.motor,
            confianca=float(ocr.confianca.valor),
            qualidade=ocr.qualidade.value,
            paginas_ignoradas=carregado.paginas_truncadas,
        )
        return ResultadoExtracao(ocr=ocr, paginas_ignoradas=carregado.paginas_truncadas)
