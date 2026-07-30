"""Validacao de token de acesso — modulo **opcional** (extra `auth`).

## O que este modulo e, e principalmente o que ele nao e

Ele valida token. Ele **nao emite** token, e nao ha aqui nenhum `/oauth/token`.

Os tres servicos sao *resource servers*, nunca *authorization servers*. A distincao nao e
vocabulario: um servico que emite o proprio token e a autoridade sobre quem ele mesmo deixa
entrar, o que significa que comprometer o servico e comprometer a identidade. Num banco o
emissor e o IdP corporativo, e cada servico so precisa saber verificar assinatura.

Para rodar local sem conta em provedor nenhum, ha `plataforma.emissor_local` — um emissor
de desenvolvimento, separado de proposito. Ele mora em outro modulo para que ninguem o
importe por acidente em codigo de producao, e o proprio modulo se recusa a rodar fora de
ambiente local.

## Por que na biblioteca compartilhada

Mesmo argumento que justificou `seguranca` aqui, e ele vale ainda mais: **defesa nao deve
divergir**. Tres servicos com tres validacoes de token significam que o mais frouxo e a
porta de entrada, e ninguem percebe, porque cada um parece protegido isoladamente.

Concretamente, sao quatro erros classicos que um deles cometeria sozinho, e os quatro tem
teste neste pacote:

1. nao fixar o algoritmo — permite `alg: none` e confusao RS256/HS256;
2. nao validar `aud` — token de um servico vale no outro;
3. nao validar `iss` — token de qualquer emissor com chave conhecida passa;
4. buscar JWKS a cada requisicao — transforma o IdP em ponto unico de falha por latencia.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, cast

import jwt
import structlog
from jwt import PyJWKClient

if TYPE_CHECKING:
    # Somente para tipagem: `Options` e um `TypedDict` do PyJWT e um import em runtime
    # amarraria este modulo a um caminho interno da biblioteca. O `cast` abaixo afirma que o
    # dicionario montado das constantes tem a forma esperada, e `test_assinatura_nunca_e_
    # dispensada` e quem garante o **conteudo**.
    from jwt.types import Options

logger = structlog.get_logger(__name__)

# Ganchos de observacao, mesmo contrato do `seguranca`: `(evento, motivo) -> None`.
#
# O servico decide o que medir. Excecao de um observador e **engolida** — falha ao medir nao
# pode transformar uma requisicao legitima em erro, nem deixar passar uma ilegitima.
_Observador = Callable[[str, str], None]
_observadores: list[_Observador] = []


def registrar_observador(observador: _Observador) -> None:
    """Registra um callback chamado a cada decisao de autenticacao."""
    _observadores.append(observador)


def limpar_observadores() -> None:
    """Remove todos os observadores — usado pela suite para nao vazar estado."""
    _observadores.clear()


def _notificar(evento: str, motivo: str) -> None:
    for observador in _observadores:
        try:
            observador(evento, motivo)
        except Exception:
            logger.warning("autenticacao.observador_falhou", exc_info=True)


# **Um** algoritmo, fixo.
#
# `jwt.decode` aceita uma lista de algoritmos permitidos. Passar o `alg` que vem no header
# do proprio token — que e o que uma implementacao ingenua faz — habilita dois ataques:
#
#   `alg: none`   token sem assinatura aceito como valido;
#   `alg: HS256`  o atacante assina com a **chave publica** (que e publica) como se fosse
#                 segredo HMAC, e a biblioteca a usa para verificar.
#
# ## O que esta linha realmente sustenta, medido
#
# A primeira versao deste comentario chamava isto de "a linha mais importante do modulo". Um
# teste de mutacao mostrou que isso estava errado, e vale registrar o que foi medido com os
# dois tokens forjados de `tests/test_autenticacao.py`:
#
#   algorithms=["RS256"]            InvalidAlgorithmError  <- esta lista barra, e da o erro certo
#   algorithms=["RS256","HS256"]    InvalidKeyError        <- o PyJWT ainda barra
#   options={"verify_signature": False}   **ACEITO**, sub=atacante
#
# Ou seja: com a lista aberta, o PyJWT continua recusando, porque `HMACAlgorithm` se nega a
# usar chave assimetrica como segredo HMAC — no lado de quem verifica, nao so no de quem
# assina. Essa segunda barreira e real e nao e nossa.
#
# Ela tambem **desapareceria** no dia em que alguem trocasse para um segredo simetrico. Ou
# seja, o que sustenta a defesa aqui e a escolha de RS256 (chave assimetrica) somada ao
# `verify_signature` explicito; a lista fixa e defesa em profundidade que produz a mensagem
# de erro correta em vez de uma sobre formato de chave.
#
# Assimetrico e nao HS256, entao, por dois motivos: o segredo simetrico teria de existir em
# todo servico que valida — cada resource server viraria capaz de **emitir** token — e sua
# presenca removeria a barreira que o PyJWT oferece de graca.
ALGORITMOS = ("RS256",)

# Opcoes de verificacao como constante, em vez de literal dentro do `jwt.decode`.
#
# Explicito em vez de confiar no default da biblioteca: default muda entre versoes, e uma
# atualizacao que desligasse `verify_aud` nao quebraria nenhum teste que nao verificasse
# isto de proposito.
#
# E constante nomeada, e nao dicionario inline, por um motivo aprendido na tentativa
# anterior: o teste que garante `verify_signature` ativo comecou lendo o **codigo-fonte**
# deste arquivo com `in`, e falhou casando com o proprio comentario acima que documenta o
# ataque. Mesma classe do `grep -A 3 "limits:"` que acusou os tres servicos de um limite de
# CPU inexistente nos manifests. Valor de configuracao que precisa de teste tem de ser
# inspecionavel em runtime, nao procurado como texto.
# Duas constantes e nao um dicionario unico: o `--strict` reclama de `dict[str, object]` na
# fronteira do `jwt.decode`, e um `Any` para calar o mypy num modulo de autenticacao e
# exatamente onde nao se quer perder tipagem.
VERIFICACOES: Final[dict[str, bool]] = {
    "verify_signature": True,
    "verify_exp": True,
    "verify_aud": True,
    "verify_iss": True,
}

# `require` fecha o buraco de claim ausente. Sem `exp` o token nunca expira; sem `sub` nao ha
# o que registrar na trilha de auditoria; sem `aud` a validacao de audiencia nao tem o que
# comparar e passa.
CLAIMS_OBRIGATORIAS: Final[tuple[str, ...]] = ("exp", "iat", "iss", "aud", "sub")

# Tolerancia de relogio. Sem ela, alguns segundos de deriva entre o emissor e o servico
# rejeitam token recem-emitido com "ainda nao valido" (`nbf`) — falha intermitente que
# aparece em producao e nao no laboratorio, porque em laboratorio o relogio e o mesmo.
#
# 30s e o teto: acima disso a janela em que um token revogado continua aceito cresce sem
# ganho de robustez.
FOLGA_DE_RELOGIO_SEGUNDOS = 30

# Cache do JWKS. Sem cache, cada requisicao vira uma chamada HTTP ao IdP: a latencia dele
# entra na de toda requisicao e uma indisponibilidade momentanea derruba a autenticacao
# inteira. Com cache, a rotacao de chave leva no maximo este tempo para ser vista.
CACHE_JWKS_SEGUNDOS = 300


class ErroDeAutenticacao(Exception):
    """Base. Nao carrega detalhe destinado ao cliente — ver `motivo`."""

    # `motivo` e um dominio **fechado**, e a razao e dupla: ele vai para label de metrica
    # (cardinalidade) e para o log (nao pode conter conteudo do token).
    motivo = "desconhecido"


class TokenAusente(ErroDeAutenticacao):
    """Nenhuma credencial apresentada. Vira 401."""

    motivo = "ausente"


class TokenInvalido(ErroDeAutenticacao):
    """Assinatura, expiracao, emissor ou audiencia incorretos. Vira 401."""

    motivo = "invalido"


class TokenExpirado(TokenInvalido):
    """Separado do invalido generico porque a acao do cliente e outra: renovar."""

    motivo = "expirado"


class AudienciaIncorreta(TokenInvalido):
    """Token emitido para outro servico.

    Merece motivo proprio porque num monorepo com tres servicos e o erro de integracao mais
    provavel — e, se a validacao de `aud` estivesse ausente, seria uma escalada lateral:
    token do canal de atendimento consultando analise de credito.
    """

    motivo = "audiencia_incorreta"


class EscopoInsuficiente(ErroDeAutenticacao):
    """Identidade valida, permissao ausente. Vira **403**, nunca 401.

    A distincao e observavel pelo cliente e importa: 401 diz "suas credenciais nao servem,
    tente outras"; 403 diz "suas credenciais servem e nao bastam". Devolver 401 aqui manda
    um cliente correto tentar reautenticar num laco que nunca resolve.
    """

    motivo = "escopo_insuficiente"


@dataclass(frozen=True, slots=True)
class Identidade:
    """Quem esta chamando, ja validado.

    Frozen porque nada a jusante deve poder adicionar escopo a uma identidade em memoria —
    seria uma escalada de privilegio de uma linha, dificil de ver em revisao.
    """

    sujeito: str
    """`sub` do token: o cliente OAuth, nao a pessoa. Vai para a trilha de auditoria."""

    escopos: frozenset[str] = field(default_factory=frozenset)
    emissor: str = ""
    audiencia: str = ""
    expira_em: int = 0

    # Identificador do locatario, quando o emissor o fornece. Fica **fora** de `escopos`
    # de proposito: escopo responde "pode fazer isso?" e locatario responde "sobre qual
    # conjunto de dados?". Misturar os dois produz o bug de um cliente com o escopo certo
    # lendo o dado de outro.
    locatario: str | None = None

    def tem(self, escopo: str) -> bool:
        return escopo in self.escopos

    def exigir(self, escopo: str) -> None:
        """Levanta `EscopoInsuficiente` se faltar. Nao ha versao que devolva bool e siga."""
        if not self.tem(escopo):
            _notificar("escopo_negado", escopo)
            raise EscopoInsuficiente(f"escopo ausente: {escopo}")


class Chaveiro:
    """Fonte das chaves publicas de verificacao.

    Duas formas de construir, e as duas existem por necessidade real:

    - `de_jwks(url)` — producao. Busca o JWKS do IdP e cacheia, o que permite **rotacao de
      chave sem redeploy**: o emissor publica a nova, os servicos a veem no proximo ciclo
      de cache.
    - `de_chave_publica(pem)` — desenvolvimento e teste. Sem servidor HTTP no meio, o que
      mantem a suite rapida e deixa o compose funcionar sem um IdP no ar.

    A segunda forma nao e um atalho preguicoso: o que ela **nao** faz e aceitar chave por
    variavel de ambiente em producao, onde a rotacao exigiria reiniciar os tres servicos ao
    mesmo tempo. Por isso a configuracao de cada servico exige uma das duas e recusa as
    duas juntas — ambiguidade sobre qual chave manda e como se aceita token que deveria ter
    sido rejeitado.
    """

    __slots__ = ("_cliente_jwks", "_pem")

    def __init__(self, *, cliente_jwks: PyJWKClient | None, pem: str | None) -> None:
        if (cliente_jwks is None) == (pem is None):
            raise ValueError("informe exatamente uma fonte de chave: JWKS ou PEM")
        self._cliente_jwks = cliente_jwks
        self._pem = pem

    @classmethod
    def de_jwks(cls, url: str, *, cache_segundos: int = CACHE_JWKS_SEGUNDOS) -> Chaveiro:
        return cls(
            cliente_jwks=PyJWKClient(url, cache_keys=True, lifespan=cache_segundos),
            pem=None,
        )

    @classmethod
    def de_chave_publica(cls, pem: str) -> Chaveiro:
        if "PRIVATE KEY" in pem:
            # Guarda contra o erro que transformaria os resource servers em emissores: uma
            # chave privada aqui e a capacidade de assinar token, nao de verificar.
            raise ValueError("chave PRIVADA entregue ao verificador; use a publica")
        return cls(cliente_jwks=None, pem=pem)

    def chave_para(self, token: str) -> Any:
        if self._pem is not None:
            return self._pem
        assert self._cliente_jwks is not None
        # `get_signing_key_from_jwt` le o `kid` do header para escolher a chave. Isso e
        # seguro porque `kid` so seleciona entre chaves que **o IdP publicou** — diferente
        # de `alg`, que selecionaria o algoritmo de verificacao.
        return self._cliente_jwks.get_signing_key_from_jwt(token).key


def verificar(
    token: str | None,
    *,
    chaveiro: Chaveiro,
    emissor: str,
    audiencia: str,
    escopos_exigidos: Sequence[str] = (),
    agora: Callable[[], float] = time.time,
) -> Identidade:
    """Valida o token e devolve a identidade, ou levanta `ErroDeAutenticacao`.

    `emissor` e `audiencia` sao **obrigatorios** e nao tem default. Um default vazio
    passaria silenciosamente por `verify_aud=False`, e a falha de validacao de audiencia e
    invisivel: tudo funciona, inclusive o que nao deveria.

    `agora` e injetavel para que os testes de expiracao nao dependam de dormir nem do
    relogio da maquina — foi um teste dependente de relogio que ja apareceu neste projeto,
    na camada de observabilidade.
    """
    if not token:
        _notificar("negado", TokenAusente.motivo)
        raise TokenAusente("credencial ausente")

    try:
        conteudo: Mapping[str, Any] = jwt.decode(
            token,
            key=chaveiro.chave_para(token),
            # Lista fixa, nunca o `alg` do header. Ver `ALGORITMOS`.
            algorithms=list(ALGORITMOS),
            audience=audiencia,
            issuer=emissor,
            leeway=FOLGA_DE_RELOGIO_SEGUNDOS,
            # Montado a cada chamada a partir das constantes. `jwt.decode` nao muta o
            # dicionario hoje, mas passar a constante direto deixaria uma versao futura que
            # mutasse capaz de alterar a politica do processo inteiro na primeira requisicao.
            options=cast("Options", {**VERIFICACOES, "require": list(CLAIMS_OBRIGATORIAS)}),
        )
    except jwt.ExpiredSignatureError as exc:
        _notificar("negado", TokenExpirado.motivo)
        raise TokenExpirado("token expirado") from exc
    except jwt.InvalidAudienceError as exc:
        _notificar("negado", AudienciaIncorreta.motivo)
        raise AudienciaIncorreta("token emitido para outra audiencia") from exc
    except jwt.PyJWTError as exc:
        # Bloco largo de proposito, e **a mensagem original nao vai para o cliente**: ela
        # distingue "assinatura invalida" de "emissor desconhecido" de "campo ausente", e
        # essa diferenca e util para quem esta sondando o servico. Vai para o log.
        logger.info("autenticacao.token_rejeitado", erro=type(exc).__name__)
        _notificar("negado", TokenInvalido.motivo)
        raise TokenInvalido("token invalido") from exc

    identidade = _montar_identidade(conteudo)

    for escopo in escopos_exigidos:
        identidade.exigir(escopo)

    _notificar("aceito", "ok")
    return identidade


def _montar_identidade(conteudo: Mapping[str, Any]) -> Identidade:
    """Traduz as claims em `Identidade`.

    O `scope` do OAuth 2.0 (RFC 8693) e **uma string separada por espaco**, nao uma lista.
    Aceitar as duas formas e deliberado: alguns IdPs emitem `scp` como array. Tratar apenas
    a string faria os escopos de um emissor virarem lista vazia — e lista vazia de escopo
    nao falha de forma visivel, ela apenas nega tudo, o que parece problema de configuracao
    de permissao e nao de parsing.
    """
    bruto = conteudo.get("scope") or conteudo.get("scp") or ""
    if isinstance(bruto, str):
        escopos = frozenset(bruto.split())
    elif isinstance(bruto, Sequence):
        escopos = frozenset(str(item) for item in bruto)
    else:
        escopos = frozenset()

    audiencia = conteudo["aud"]
    if not isinstance(audiencia, str):
        # `aud` pode ser lista. O `jwt.decode` ja confirmou que a nossa esta dentro; aqui
        # so escolhemos um valor escalar para o log e para a metrica.
        audiencia = next(iter(audiencia), "")

    locatario = conteudo.get("tenant") or conteudo.get("locatario")

    return Identidade(
        sujeito=str(conteudo["sub"]),
        escopos=escopos,
        emissor=str(conteudo["iss"]),
        audiencia=str(audiencia),
        expira_em=int(conteudo["exp"]),
        locatario=str(locatario) if locatario else None,
    )


def extrair_do_cabecalho(valor: str | None) -> str | None:
    """Tira o token de um `Authorization: Bearer <token>`.

    Devolve `None` em vez de levantar quando o cabecalho esta ausente ou malformado: quem
    decide o que fazer com a ausencia e o `verificar`, que tem o `motivo` fechado. Duas
    fontes levantando o mesmo erro de formas diferentes e como o 401 e o 403 se confundem.

    O esquema e comparado sem diferenciar caixa porque a RFC 7235 o define como
    case-insensitive — `bearer` minusculo e valido, e um cliente que use isso receberia 401
    sem explicacao possivel.
    """
    if not valor:
        return None
    partes = valor.split(maxsplit=1)
    if len(partes) != 2 or partes[0].lower() != "bearer":
        return None
    return partes[1].strip() or None
