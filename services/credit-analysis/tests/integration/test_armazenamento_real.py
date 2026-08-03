"""Os adapters de S3 e SQS contra MinIO e ElasticMQ de verdade.

## Por que estes testes existem, se ja ha os de memoria

Porque os adapters em memoria **implementam o que eu decidi**, e estes verificam o que o protocolo
de fato faz. As duas coisas divergem em pontos que importam:

- o `VersionId` do S3 vem no `put_object` e **nao** no `upload_fileobj` de alto nivel. Um teste com
  fake nunca descobriria isso;
- `ApproximateReceiveCount` so aparece se pedido explicitamente em `MessageSystemAttributeNames`;
- `change_message_visibility(0)` devolve a mensagem preservando a contagem, enquanto reenviar
  criaria mensagem nova com contagem zerada — e a DLQ nunca seria alcancada.

Nenhum desses tres seria pego por um fake que eu escrevi a partir da minha propria leitura da
documentacao.

## Pulados quando os servicos nao estao no ar

Pela mesma disciplina dos testes de pgvector: skip com **motivo legivel**, e o CI falha se o motivo
aparecer — um teste pulado em silencio da a impressao de cobertura que nao existe.
"""

from __future__ import annotations

import io
import os
import uuid
from collections.abc import Iterator

import boto3
import pytest
from botocore.exceptions import EndpointConnectionError

from credit_analysis.domain.armazenamento import Referencia
from credit_analysis.domain.enums import TipoDocumento
from credit_analysis.domain.extracao_assincrona import PedidoDeExtracao
from credit_analysis.infrastructure.armazenamento.s3 import (
    ArmazenamentoS3,
    ReferenciaInvalida,
    VersionamentoDesabilitado,
)
from credit_analysis.infrastructure.armazenamento.sqs import FilaSQS

pytestmark = pytest.mark.integration

# Endpoints locais. Vazio = pula, com motivo.
S3_ENDPOINT = os.getenv("CREDIT_S3_ENDPOINT_TEST", "")
SQS_ENDPOINT = os.getenv("CREDIT_SQS_ENDPOINT_TEST", "")

MOTIVO_S3 = "CREDIT_S3_ENDPOINT_TEST nao definido (suba o MinIO: docker compose up -d minio)"
MOTIVO_SQS = (
    "CREDIT_SQS_ENDPOINT_TEST nao definido (suba o ElasticMQ: docker compose up -d elasticmq)"
)


def _credenciais() -> dict[str, str]:
    """O boto3 exige credencial mesmo contra MinIO/ElasticMQ.

    Sem ela, `NoCredentialsError` — que numa suite parece falha do adapter e nao do ambiente.
    """
    return {
        "aws_access_key_id": os.getenv("AWS_ACCESS_KEY_ID", "credito"),
        "aws_secret_access_key": os.getenv("AWS_SECRET_ACCESS_KEY", "credito_local_minio"),
    }


@pytest.fixture
def bucket_versionado() -> Iterator[str]:
    """Bucket temporario **com** versionamento, apagado no fim.

    Nome com UUID: um bucket fixo faria duas execucoes simultaneas da suite interferirem, e o
    sintoma seria um teste que falha so quando alguem mais esta rodando.
    """
    if not S3_ENDPOINT:
        pytest.skip(MOTIVO_S3)

    cliente = boto3.client(
        "s3", endpoint_url=S3_ENDPOINT, region_name="sa-east-1", **_credenciais()
    )
    nome = f"teste-{uuid.uuid4().hex[:12]}"
    try:
        cliente.create_bucket(Bucket=nome)
    except EndpointConnectionError:
        pytest.skip(f"{MOTIVO_S3} (endpoint {S3_ENDPOINT} nao responde)")

    cliente.put_bucket_versioning(Bucket=nome, VersioningConfiguration={"Status": "Enabled"})
    yield nome

    # Limpeza: apagar **todas as versoes**, nao apenas os objetos.
    #
    # Num bucket versionado, `delete_object` cria um marcador de exclusao e o bucket continua nao
    # vazio — o `delete_bucket` falharia. Foi assim que a primeira versao deste fixture deixou
    # buckets orfaos acumulando no MinIO local.
    versoes = cliente.list_object_versions(Bucket=nome)
    for grupo in ("Versions", "DeleteMarkers"):
        for item in versoes.get(grupo, []):
            cliente.delete_object(Bucket=nome, Key=item["Key"], VersionId=item["VersionId"])
    cliente.delete_bucket(Bucket=nome)


@pytest.fixture
def bucket_sem_versionamento() -> Iterator[str]:
    if not S3_ENDPOINT:
        pytest.skip(MOTIVO_S3)

    cliente = boto3.client(
        "s3", endpoint_url=S3_ENDPOINT, region_name="sa-east-1", **_credenciais()
    )
    nome = f"teste-sv-{uuid.uuid4().hex[:12]}"
    try:
        cliente.create_bucket(Bucket=nome)
    except EndpointConnectionError:
        pytest.skip(f"{MOTIVO_S3} (endpoint {S3_ENDPOINT} nao responde)")

    yield nome

    for item in cliente.list_objects_v2(Bucket=nome).get("Contents", []):
        cliente.delete_object(Bucket=nome, Key=item["Key"])
    cliente.delete_bucket(Bucket=nome)


@pytest.fixture
def armazenamento(bucket_versionado: str) -> ArmazenamentoS3:
    return ArmazenamentoS3(bucket=bucket_versionado, endpoint_url=S3_ENDPOINT)


@pytest.fixture
def fila() -> Iterator[FilaSQS]:
    """Fila **propria** por teste, criada e apagada.

    A primeira versao usava a fila `extracao-documentos` do `elasticmq.conf`, e os testes passavam.
    Depois falharam — porque a stack do compose estava no ar, com o trabalhador em processo
    consumindo daquela mesma fila e roubando as mensagens.

    O sintoma era enganoso: tres testes de fila falhando juntos parece bug no adapter, e o adapter
    estava correto. A causa era o teste compartilhar recurso com um processo de verdade.

    Nome com UUID, como no bucket. O `elasticmq.conf` continua declarando a fila de producao local
    com a DLQ — o que estes testes nao verificam, porque `maxReceiveCount` e comportamento da fila
    e nao do adapter.
    """
    if not SQS_ENDPOINT:
        pytest.skip(MOTIVO_SQS)

    cliente = boto3.client(
        "sqs", endpoint_url=SQS_ENDPOINT, region_name="sa-east-1", **_credenciais()
    )
    nome = f"teste-{uuid.uuid4().hex[:12]}"
    try:
        url = cliente.create_queue(QueueName=nome, Attributes={"VisibilityTimeout": "300"})[
            "QueueUrl"
        ]
    except EndpointConnectionError:
        pytest.skip(f"{MOTIVO_SQS} (endpoint {SQS_ENDPOINT} nao responde)")

    # O ElasticMQ devolve a URL com o host que **ele** conhece, que dentro do container e outro.
    # Reescrever pelo endpoint do teste evita um erro de conexao que pareceria falha do adapter.
    url = f"{SQS_ENDPOINT}/000000000000/{nome}"

    yield FilaSQS(url_da_fila=url, endpoint_url=SQS_ENDPOINT)

    cliente.delete_queue(QueueUrl=url)


def pedido(documento_id: uuid.UUID | None = None) -> PedidoDeExtracao:
    return PedidoDeExtracao(
        analise_id=uuid.uuid4(),
        documento_id=documento_id or uuid.uuid4(),
        referencia=Referencia(chave="documentos/a/b/x.png", versao="v1"),
        tipo=TipoDocumento.HOLERITE,
        nome_arquivo="x.png",
        request_id="teste-correlacao",
    )


class TestArmazenamentoS3:
    async def test_guarda_e_recupera(self, armazenamento: ArmazenamentoS3) -> None:
        referencia = await armazenamento.guardar(
            "documentos/a/b/holerite.png", io.BytesIO(b"conteudo-original"), "image/png"
        )

        assert referencia.versao, "o S3 nao devolveu VersionId"
        assert await armazenamento.obter(referencia) == b"conteudo-original"

    async def test_a_versao_fixa_o_conteudo(self, armazenamento: ArmazenamentoS3) -> None:
        """O teste que justifica a `Referencia` carregar versao.

        Duas gravacoes na **mesma chave**. Se `obter` lesse "a versao atual", a primeira referencia
        passaria a devolver o segundo conteudo — e o parecer citaria um documento que nao foi o
        extraido.

        E o cenario real de um reenvio depois de rejeicao por qualidade.
        """
        chave = "documentos/a/b/holerite.png"
        primeira = await armazenamento.guardar(chave, io.BytesIO(b"primeiro-envio"), "image/png")
        segunda = await armazenamento.guardar(chave, io.BytesIO(b"segundo-envio"), "image/png")

        assert primeira.versao != segunda.versao
        assert await armazenamento.obter(primeira) == b"primeiro-envio"
        assert await armazenamento.obter(segunda) == b"segundo-envio"

    async def test_bucket_sem_versionamento_levanta_em_vez_de_gravar(
        self, bucket_sem_versionamento: str
    ) -> None:
        """O modo de falha silencioso que este adapter recusa.

        Sem versionamento, o `put_object` **funciona** e a resposta simplesmente nao tem
        `VersionId`. O boto3 nao levanta.

        Aceitar isso daria referencia com versao vazia, deduplicacao por versao virando dedupe por
        chave, e reenvio legitimo descartado como duplicata. Nada falharia — o sistema so perderia
        documento de vez em quando.
        """
        adapter = ArmazenamentoS3(bucket=bucket_sem_versionamento, endpoint_url=S3_ENDPOINT)

        with pytest.raises(VersionamentoDesabilitado, match="versionamento"):
            await adapter.guardar("x.png", io.BytesIO(b"dados"), "image/png")

    async def test_confere_versionamento_no_boot(
        self, armazenamento: ArmazenamentoS3, bucket_sem_versionamento: str
    ) -> None:
        """A checagem que o `criar_app` usa: falha na subida, nao na primeira gravacao.

        Sem ela, um bucket mal configurado passaria pelo `/ready` e falharia no primeiro upload de
        verdade — em producao, com um cliente do outro lado.
        """
        assert await armazenamento.conferir_versionamento() is True

        sem = ArmazenamentoS3(bucket=bucket_sem_versionamento, endpoint_url=S3_ENDPOINT)
        assert await sem.conferir_versionamento() is False

    async def test_objeto_ausente_e_filenotfound(self, armazenamento: ArmazenamentoS3) -> None:
        """Objeto que nao existe, com version id **bem formado**.

        `FileNotFoundError` e nao `ClientError`: o trabalhador classifica por tipo, e objeto ausente
        e **transitorio** (pode ser consistencia eventual do S3).

        A versao usada e um UUID sem hifens, o formato que o MinIO gera. A primeira versao deste
        teste passava a string `"inexistente"`, e o MinIO devolveu `InvalidArgument` em vez de
        `NoSuchVersion` — dois casos diferentes que o teste estava confundindo num so.
        """
        with pytest.raises(FileNotFoundError):
            await armazenamento.obter(Referencia(chave="nao/existe.png", versao=uuid.uuid4().hex))

    async def test_referencia_malformada_e_permanente(self, armazenamento: ArmazenamentoS3) -> None:
        """O caso que o MinIO revelou, e que estava classificado errado.

        Version id que nem tem formato valido nunca vira valido. Tratado como transitorio, ele
        gastaria tres tentativas de OCR para chegar a uma conclusao certa na primeira — e o log
        diria "falha transitoria" sobre algo definitivo.
        """
        with pytest.raises(ReferenciaInvalida):
            await armazenamento.obter(Referencia(chave="nao/existe.png", versao="nao-e-um-id"))


class TestFilaSQS:
    async def test_publica_consome_e_confirma(self, fila: FilaSQS) -> None:
        enviado = pedido()
        await fila.publicar(enviado)

        entregas = await fila.consumir(quantidade=10, espera_segundos=2)
        minhas = [e for e in entregas if e.pedido.documento_id == enviado.documento_id]
        assert minhas, "a mensagem publicada nao voltou"

        entrega = minhas[0]
        assert entrega.pedido == enviado, "o contrato nao sobreviveu a ida e volta"
        # `request_id` sobrevive: e o que mantem a trilha sob o mesmo identificador quando o
        # trabalho atravessa processos.
        assert entrega.pedido.request_id == "teste-correlacao"

        await fila.confirmar(entrega)

        # As outras entregas voltam para a fila, para nao poluir execucoes seguintes.
        for outra in entregas:
            if outra is not entrega:
                await fila.devolver(outra, "nao e minha")

    async def test_a_contagem_de_tentativas_vem_da_fila_e_sobrevive_a_devolucao(
        self, fila: FilaSQS
    ) -> None:
        """`ApproximateReceiveCount`, e por que ele nao e contado do nosso lado.

        A contagem da fila atravessa reinicio do trabalhador; a nossa nao. Com contagem local, um
        documento que ja consumiu tres das cinco tentativas voltaria a ter cinco depois de um
        deploy, e a DLQ demoraria mais para receber o que nunca vai funcionar.

        O teste tambem prova que `devolver` **preserva** a contagem: reenviar a mensagem em vez de
        liberar a visibilidade criaria mensagem nova com contagem zerada, e o `maxReceiveCount`
        nunca seria alcancado.
        """
        enviado = pedido()
        await fila.publicar(enviado)

        primeira = await self._minha(fila, enviado.documento_id)
        assert primeira.tentativas == 1

        await fila.devolver(primeira, "simulando falha transitoria")

        segunda = await self._minha(fila, enviado.documento_id)
        assert segunda.tentativas == 2, "a contagem reiniciou — `devolver` criou mensagem nova?"

        await fila.confirmar(segunda)

    async def test_mensagem_confirmada_nao_volta(self, fila: FilaSQS) -> None:
        enviado = pedido()
        await fila.publicar(enviado)

        entrega = await self._minha(fila, enviado.documento_id)
        await fila.confirmar(entrega)

        # Espera curta: se ela voltasse, voltaria imediatamente (visibilidade nao expirou).
        entregas = await fila.consumir(quantidade=10, espera_segundos=1)
        assert not [e for e in entregas if e.pedido.documento_id == enviado.documento_id]
        for outra in entregas:
            await fila.devolver(outra, "nao e minha")

    @staticmethod
    async def _minha(fila: FilaSQS, documento_id: uuid.UUID) -> object:
        """Consome ate achar a mensagem deste teste, devolvendo as alheias.

        A fila e compartilhada entre os testes deste arquivo e pode ter sobras. Filtrar em vez de
        assumir "a proxima e minha" e o que impede um teste de falhar por causa de outro — e
        `devolver` as alheias e o que impede este teste de consumi-las.
        """
        for _ in range(10):
            for entrega in await fila.consumir(quantidade=10, espera_segundos=2):
                if entrega.pedido.documento_id == documento_id:
                    return entrega
                await fila.devolver(entrega, "nao e minha")
        raise AssertionError(f"mensagem de {documento_id} nao apareceu")
