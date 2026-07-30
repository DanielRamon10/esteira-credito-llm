"""Emissor de token para desenvolvimento e teste — **nunca** para producao.

## Por que existe

O projeto tem uma restricao declarada: roda inteiro sem conta em provedor nenhum e sem
chave paga. Um IdP de verdade (Cognito, Auth0, Keycloak) contraria isso. Sem emissor
algum, a Camada 7 seria codigo de validacao que nunca valida nada de verdade — e
autenticacao que nao foi exercitada contra token real e decoracao.

Este modulo fecha a lacuna: gera um par de chaves na maquina e assina token com ele.

## Por que ele nao pode virar emissor de producao, estruturalmente

A guarda **nao** e um `if ambiente == "prod"`. Isso seria teatro: uma variavel de ambiente
errada, e o servico passa a emitir os proprios tokens.

A garantia e outra, e e fisica: **a chave privada nunca entra no repositorio.** Ela e
gerada em `.chaves/`, que esta no `.gitignore`, e sem chave privada este modulo nao
consegue assinar nada. Em producao nao existe `.chaves/`, entao nao ha o que assinar com.

Duas defesas somadas a isso:

- o `.githooks/pre-commit` deteccao de PEM impede commitar a chave por acidente — e essa
  deteccao esteve **quebrada** desde o primeiro commit (`git grep -E "-----BEGIN..."` lia
  o `-----` como opcao e precisava de `-e`), o que e exatamente o tipo de defesa que
  parece existir e nao existe;
- o CI verifica que nenhum `services/*/src/` importa este modulo. Codigo de aplicacao nao
  tem motivo para conhecer o emissor, e o dia em que tiver, o CI falha.

## Por que RSA e nao um segredo HMAC

Com HS256, o segredo de verificacao **e** o segredo de assinatura: todo servico que valida
token passaria a ser capaz de emitir token. Um comprometimento do servico mais exposto — o
`customer-support`, que fala com o publico — daria ao atacante a capacidade de forjar
identidade para consultar analise de credito.

Com RS256 os servicos recebem apenas a chave publica. Comprometer um deles nao produz
token valido para nenhum.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

# Emissor e nome de arquivo padrao. `local` no nome do emissor de proposito: se um token
# destes vazar para um log de producao, o `iss` denuncia a origem imediatamente.
EMISSOR_LOCAL = "https://local.esteira-credito.invalid"

# `.invalid` e um TLD reservado pela RFC 2606 e **nunca resolve**. Um emissor com dominio
# plausivel convidaria alguem a apontar o `jwks_uri` para ele.

DIRETORIO_PADRAO = Path(".chaves")
NOME_PRIVADA = "privada.pem"
NOME_PUBLICA = "publica.pem"

# 2048 bits: o minimo aceito hoje para RSA. 4096 seria mais forte e nada aqui justifica o
# custo — a chave e de desenvolvimento e o gargalo de assinatura apareceria na suite.
TAMANHO_CHAVE = 2048

# Uma hora. Curto o suficiente para que um token esquecido num arquivo de teste deixe de
# funcionar, longo o suficiente para uma sessao de desenvolvimento.
VALIDADE_PADRAO_SEGUNDOS = 3600


def gerar_chaves(diretorio: Path = DIRETORIO_PADRAO, *, sobrescrever: bool = False) -> Path:
    """Gera o par de chaves. Devolve o diretorio.

    Recusa sobrescrever por padrao: regenerar a chave invalida todo token em circulacao, e
    num ambiente de desenvolvimento compartilhado isso quebra o trabalho de outra pessoa
    sem aviso.
    """
    diretorio.mkdir(parents=True, exist_ok=True)
    privada = diretorio / NOME_PRIVADA
    publica = diretorio / NOME_PUBLICA

    if privada.exists() and not sobrescrever:
        return diretorio

    chave = rsa.generate_private_key(public_exponent=65537, key_size=TAMANHO_CHAVE)

    privada.write_bytes(
        chave.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            # Sem senha: a protecao desta chave e nao estar no repositorio, e uma senha
            # guardada ao lado da chave que ela protege nao protege nada. Em producao a
            # chave de assinatura vive num HSM ou no KMS, onde ela nao e exportavel.
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    publica.write_bytes(
        chave.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return diretorio


def emitir(
    *,
    audiencia: str,
    escopos: list[str],
    sujeito: str = "cliente-de-desenvolvimento",
    locatario: str | None = None,
    validade_segundos: int = VALIDADE_PADRAO_SEGUNDOS,
    diretorio: Path = DIRETORIO_PADRAO,
    emissor: str = EMISSOR_LOCAL,
    agora: int | None = None,
) -> str:
    """Assina um token com a chave local.

    `audiencia` e obrigatoria e sem default: um token sem audiencia definida valeria em
    qualquer servico, que e precisamente a escalada lateral que a validacao de `aud`
    existe para impedir. Deixar isso como default seria construir a lacuna na ferramenta
    que serve para testar a ausencia dela.
    """
    privada = diretorio / NOME_PRIVADA
    if not privada.exists():
        raise FileNotFoundError(
            f"chave privada ausente em {privada}. "
            "Rode: python -m plataforma.emissor_local gerar-chaves"
        )

    instante = int(time.time()) if agora is None else agora
    conteudo: dict[str, object] = {
        "iss": emissor,
        "sub": sujeito,
        "aud": audiencia,
        "iat": instante,
        "exp": instante + validade_segundos,
        # String separada por espaco, como manda o OAuth 2.0. Emitir como lista aqui faria
        # a suite validar um formato que a maioria dos IdPs nao usa.
        "scope": " ".join(escopos),
    }
    if locatario:
        conteudo["locatario"] = locatario

    return jwt.encode(conteudo, privada.read_text(encoding="utf-8"), algorithm="RS256")


def chave_publica(diretorio: Path = DIRETORIO_PADRAO) -> str:
    caminho = diretorio / NOME_PUBLICA
    if not caminho.exists():
        raise FileNotFoundError(
            f"chave publica ausente em {caminho}. "
            "Rode: python -m plataforma.emissor_local gerar-chaves"
        )
    return caminho.read_text(encoding="utf-8")


def _cli(argv: list[str] | None = None) -> int:
    analisador = argparse.ArgumentParser(
        prog="python -m plataforma.emissor_local",
        description="Emissor de token para desenvolvimento. NAO use em producao.",
    )
    sub = analisador.add_subparsers(dest="comando", required=True)

    sub.add_parser("gerar-chaves", help="Cria .chaves/privada.pem e .chaves/publica.pem")
    sub.add_parser("chave-publica", help="Imprime a chave publica (para colar em config)")

    token = sub.add_parser("token", help="Emite um token")
    token.add_argument("--audiencia", required=True, help="credit-analysis, kyc-compliance, ...")
    token.add_argument("--escopos", default="", help="separados por espaco")
    token.add_argument("--sujeito", default="cliente-de-desenvolvimento")
    token.add_argument("--locatario", default=None)
    token.add_argument("--validade", type=int, default=VALIDADE_PADRAO_SEGUNDOS)

    args = analisador.parse_args(argv)

    if args.comando == "gerar-chaves":
        destino = gerar_chaves()
        print(f"chaves em {destino.resolve()}")
        print("Este diretorio esta no .gitignore. NAO o remova de la.")
        return 0

    if args.comando == "chave-publica":
        print(chave_publica(), end="")
        return 0

    print(
        emitir(
            audiencia=args.audiencia,
            escopos=args.escopos.split(),
            sujeito=args.sujeito,
            locatario=args.locatario,
            validade_segundos=args.validade,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
