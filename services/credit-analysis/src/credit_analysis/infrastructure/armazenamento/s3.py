"""Armazenamento de documento em S3 (ou MinIO, que fala o mesmo protocolo).

## boto3 com `asyncio.to_thread`, e nao aioboto3

O boto3 e sincrono. Chamado direto de uma corrotina, ele **para o event loop inteiro** durante o
upload — num arquivo de 32MB isso segura todas as outras requisicoes do processo.

`asyncio.to_thread` move a chamada para o pool de threads. E o mesmo padrao que o endpoint de
upload ja usa para escrever em disco, adotado la depois de o lint de seguranca do outro servico
apontar a regra ASYNC. Trocar por `aioboto3` seria mais elegante e adicionaria uma dependencia
que substitui o cliente oficial por um mantido por terceiros — para ganhar o que o `to_thread`
ja resolve.

## O versionamento nao e opcional, e a falta dele e silenciosa

`Referencia` e `(chave, versao)`, e a versao vem do `VersionId` que o S3 devolve no `put_object`.
**Num bucket sem versionamento, esse campo simplesmente nao vem** — o boto3 nao levanta, e a
resposta apenas nao tem a chave.

O efeito seria: referencia com versao vazia, deduplicacao por versao virando deduplicacao por
chave, e um reenvio legitimo do mesmo documento descartado como duplicata. Nada falharia; o
sistema so passaria a perder documentos ocasionalmente.

Por isso o `guardar` **exige** o `VersionId` e levanta quando ele falta, com a instrucao de
habilitar o versionamento. Falhar na primeira gravacao e infinitamente melhor que perder
documento em silencio.
"""

from __future__ import annotations

import asyncio
from typing import IO, TYPE_CHECKING, Any

import boto3
import structlog
from botocore.config import Config
from botocore.exceptions import ClientError

from credit_analysis.domain.armazenamento import Referencia

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client

logger = structlog.get_logger(__name__)


class VersionamentoDesabilitado(RuntimeError):
    """O bucket nao tem versionamento, e a referencia versionada depende dele."""


class ReferenciaInvalida(ValueError):
    """A referencia em si esta malformada — nao e o objeto que falta.

    ## Por que ela e separada de `FileNotFoundError`, e a diferenca custa trabalho

    As duas parecem "nao consegui ler o objeto" e tem tratamento **oposto** no trabalhador:

    - objeto ausente pode ser consistencia eventual do S3, entao e **transitorio**: devolve para a
      fila e a proxima tentativa pode achar;
    - referencia malformada nunca vai virar valida. Retentar gastaria as tentativas e mandaria o
      documento para a DLQ com o motivo errado no log.

    Descoberta testando contra o MinIO: com version id malformado ele devolve `InvalidArgument`, e
    nao `NoSuchVersion`. Sem esta distincao, os dois caiam no default transitorio — o sistema se
    recuperaria, e gastaria tres tentativas de OCR para descobrir algo que era certo na primeira.
    """


class ArmazenamentoS3:
    """Adapter do port `ArmazenamentoDocumentos` sobre S3 ou MinIO.

    `endpoint_url` existe para o MinIO. Em AWS ele fica `None` e o boto3 resolve o endereco
    regional — passar o endereco da AWS a mao funcionaria e quebraria em outra regiao.
    """

    __slots__ = ("_bucket", "_cliente", "_endpoint")

    def __init__(
        self,
        bucket: str,
        *,
        regiao: str = "sa-east-1",
        endpoint_url: str | None = None,
        cliente: Any = None,
    ) -> None:
        self._bucket = bucket
        self._endpoint = endpoint_url
        self._cliente: S3Client = cliente or boto3.client(
            "s3",
            region_name=regiao,
            endpoint_url=endpoint_url,
            config=Config(
                # `path` e nao `virtual`: o MinIO responde em `http://minio:9000/bucket/chave`,
                # e o estilo virtual (`http://bucket.minio:9000/chave`) exigiria DNS por bucket.
                # Em AWS o path style tambem funciona, entao um valor serve para os dois.
                s3={"addressing_style": "path"},
                # Retry do proprio boto3, em modo adaptativo: ele respeita o `Retry-After` do
                # servico em vez de reenviar em intervalo fixo. Sem isto, o retry do nosso
                # trabalhador e o do cliente se somariam e multiplicariam a carga num momento em
                # que o armazenamento ja esta pedindo calma.
                retries={"max_attempts": 3, "mode": "adaptive"},
                connect_timeout=5,
                read_timeout=30,
            ),
        )

    async def guardar(self, chave: str, conteudo: IO[bytes], tipo_mime: str) -> Referencia:
        def _subir() -> str:
            # `put_object` e nao `upload_fileobj`: o segundo faz upload multipart e **nao devolve
            # o `VersionId`** na resposta de alto nivel. Sem ele nao ha referencia imutavel, que
            # e a base da idempotencia.
            #
            # O preco e nao ter multipart automatico, e ele e aceitavel: o teto de upload e 32MB
            # e o limite de `put_object` e 5GB.
            resposta = self._cliente.put_object(
                Bucket=self._bucket,
                Key=chave,
                Body=conteudo,
                ContentType=tipo_mime,
                # **Sem `ServerSideEncryption` por objeto**, e a ausencia foi aprendida contra o
                # MinIO.
                #
                # A primeira versao passava `ServerSideEncryption="AES256"` aqui, argumentando que
                # cobriria um bucket criado a mao sem regra default. O MinIO recusou:
                # `NotImplemented: Server side encryption specified but KMS is not configured`.
                #
                # E o erro foi util. Criptografia em repouso e **politica do bucket**, nao decisao
                # de cada gravacao: no bucket, ela vale para todo objeto, inclusive os escritos por
                # outra ferramenta. Por objeto, ela vale so para quem lembrar de passar o header —
                # e o dia em que um script de migracao gravar sem ele, ninguem nota.
                #
                # A garantia mudou de lugar: o Terraform define a regra default e
                # `conferir_criptografia` a verifica no boot, exigida em producao.
            )
            versao = resposta.get("VersionId")
            if not versao:
                raise VersionamentoDesabilitado(
                    f"o bucket '{self._bucket}' nao devolveu VersionId. "
                    "A referencia versionada depende dele — habilite o versionamento "
                    "(aws s3api put-bucket-versioning --status Enabled)."
                )
            return str(versao)

        versao = await asyncio.to_thread(_subir)
        logger.debug("s3.guardado", bucket=self._bucket, chave=chave, versao=versao)
        return Referencia(chave=chave, versao=versao)

    async def obter(self, referencia: Referencia) -> bytes:
        def _baixar() -> bytes:
            try:
                resposta = self._cliente.get_object(
                    Bucket=self._bucket,
                    Key=referencia.chave,
                    # `VersionId` explicito. Ler "a versao atual da chave" seria o comportamento
                    # errado: entre o upload e a extracao um reenvio pode ter trocado o conteudo,
                    # e o parecer citaria um documento que nao foi o extraido.
                    VersionId=referencia.versao,
                )
            except ClientError as exc:
                codigo = exc.response.get("Error", {}).get("Code", "")
                if codigo == "InvalidArgument":
                    # Version id malformado. O MinIO responde assim; a AWS tambem, para um id que
                    # nao tem o formato dela. Permanente — ver `ReferenciaInvalida`.
                    raise ReferenciaInvalida(f"referencia malformada: {referencia}") from exc
                if codigo in ("NoSuchKey", "NoSuchVersion", "404"):
                    # `FileNotFoundError` e nao `ClientError`: o trabalhador classifica erro por
                    # tipo, e objeto ausente e **transitorio** (consistencia eventual). Deixar o
                    # `ClientError` subir tambem cairia no default transitorio, mas o tipo
                    # generico esconde o motivo de quem le o log.
                    raise FileNotFoundError(f"objeto ausente: {referencia}") from exc
                raise
            corpo: bytes = resposta["Body"].read()
            return corpo

        return await asyncio.to_thread(_baixar)

    @property
    def identificacao(self) -> str:
        destino = self._endpoint or "aws"
        return f"s3:{destino}/{self._bucket}"

    async def conferir_criptografia(self) -> bool:
        """Se o bucket tem regra de criptografia default.

        Verificada no boot e exigida **somente em producao**: localmente o MinIO roda sem KMS e nao
        ha dado real de cliente. Exigir em todo ambiente tornaria o compose dependente de
        configurar KMS para guardar um PNG de teste.
        """

        def _conferir() -> bool:
            try:
                resposta = self._cliente.get_bucket_encryption(Bucket=self._bucket)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in (
                    "ServerSideEncryptionConfigurationNotFoundError",
                    "NotImplemented",
                ):
                    return False
                raise
            regras = resposta.get("ServerSideEncryptionConfiguration", {}).get("Rules", [])
            return bool(regras)

        return await asyncio.to_thread(_conferir)

    async def conferir_versionamento(self) -> bool:
        """Confere o versionamento **no boot**, e nao na primeira gravacao.

        Sem isto, um bucket mal configurado passaria pelo `/ready` e falharia no primeiro upload
        de verdade — em producao, com um cliente do outro lado. O `criar_app` chama isto e recusa
        subir quando o versionamento esta desligado.
        """

        def _conferir() -> bool:
            resposta = self._cliente.get_bucket_versioning(Bucket=self._bucket)
            return str(resposta.get("Status", "")) == "Enabled"

        return await asyncio.to_thread(_conferir)
