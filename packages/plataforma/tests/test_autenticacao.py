"""Validacao de token.

## O que esta suite protege

Os quatro erros classicos de resource server, cada um com teste proprio. Nenhum deles
falha de forma visivel: um servico com qualquer um dos quatro **funciona**, atende
requisicao legitima, e aceita tambem a ilegitima.

1. algoritmo nao fixado -> `alg: none` e confusao RS256/HS256
2. `aud` nao validada -> token de um servico vale no outro
3. `iss` nao validado -> token de qualquer emissor com chave conhecida passa
4. claim obrigatoria ausente -> token sem `exp` nunca expira

## Nenhuma chave literal neste arquivo

As chaves sao geradas em `tmp_path` por fixture. Dois motivos, e o segundo e concreto: o
`.githooks/pre-commit` deteccao de PEM bloquearia o commit, e ja aconteceu duas vezes
neste projeto o reflexo errado de **enfraquecer o scanner** para deixar o teste passar. A
saida certa e o teste nao ter o padrao.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Iterator
from pathlib import Path

import jwt
import pytest

from plataforma import autenticacao as auth
from plataforma import emissor_local as emissor

AUDIENCIA = "credit-analysis"
OUTRA_AUDIENCIA = "kyc-compliance"


@pytest.fixture(scope="module")
def chaves(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Par de chaves proprio da suite, fora do repositorio.

    `scope="module"` porque gerar RSA de 2048 bits custa ~100ms e nada nos testes muta a
    chave. Por teste, a suite ficaria dominada por geracao de chave.
    """
    destino = tmp_path_factory.mktemp("chaves")
    return emissor.gerar_chaves(destino)


@pytest.fixture
def chaveiro(chaves: Path) -> auth.Chaveiro:
    return auth.Chaveiro.de_chave_publica(emissor.chave_publica(chaves))


@pytest.fixture
def token(chaves: Path) -> str:
    return emissor.emitir(
        audiencia=AUDIENCIA,
        escopos=["analises:ler", "analises:escrever"],
        locatario="acme",
        diretorio=chaves,
    )


@pytest.fixture(autouse=True)
def sem_observadores() -> Iterator[None]:
    """Isola os observadores entre testes.

    `autouse` e limpeza no **fim**: um teste que registre observador e falhe no meio
    deixaria o gancho ativo para os seguintes, e o sintoma seria um teste passando ou
    falhando conforme a ordem.
    """
    yield
    auth.limpar_observadores()


def _verificar(token: str | None, chaveiro: auth.Chaveiro, **kwargs: object) -> auth.Identidade:
    padroes: dict[str, object] = {
        "chaveiro": chaveiro,
        "emissor": emissor.EMISSOR_LOCAL,
        "audiencia": AUDIENCIA,
    }
    padroes.update(kwargs)
    return auth.verificar(token, **padroes)  # type: ignore[arg-type]


class TestCaminhoFeliz:
    def test_token_valido_devolve_identidade(self, token: str, chaveiro: auth.Chaveiro) -> None:
        identidade = _verificar(token, chaveiro)

        assert identidade.sujeito == "cliente-de-desenvolvimento"
        assert identidade.escopos == {"analises:ler", "analises:escrever"}
        assert identidade.emissor == emissor.EMISSOR_LOCAL
        assert identidade.locatario == "acme"

    def test_escopo_exigido_e_presente(self, token: str, chaveiro: auth.Chaveiro) -> None:
        _verificar(token, chaveiro, escopos_exigidos=["analises:ler"])

    def test_identidade_e_imutavel(self, token: str, chaveiro: auth.Chaveiro) -> None:
        """Adicionar escopo a uma identidade em memoria seria escalada de uma linha.

        `frozen=True` faz disso um erro em tempo de execucao em vez de um bug que passa em
        revisao de codigo porque a linha parece inofensiva.
        """
        identidade = _verificar(token, chaveiro)

        with pytest.raises((AttributeError, TypeError)):
            identidade.sujeito = "outro"  # type: ignore[misc]


class TestConfusaoDeAlgoritmo:
    """Os dois ataques classicos, e qual camada bloqueia cada um.

    Um teste de mutacao corrigiu o que este bloco afirmava. Trocando
    `ALGORITMOS = ("RS256",)` por `("RS256", "HS256")`, os dois testes de ataque
    **continuaram passando** — logo eles nao estavam provando o que o nome dizia.

    O que foi medido, com os mesmos tokens forjados abaixo:

        algorithms=["RS256"]                  InvalidAlgorithmError
        algorithms=["RS256","HS256"]          InvalidKeyError
        options={"verify_signature": False}   **ACEITO**, sub=atacante

    A segunda linha e o PyJWT recusando usar chave assimetrica como segredo HMAC, do lado
    de quem verifica. E defesa real, e nao e nossa — e desapareceria com um segredo
    simetrico.

    Entao estes dois testes provam o **resultado** (o ataque falha), e o
    `test_assinatura_nunca_e_dispensada` cobre a brecha que de fato abre tudo. Chamar a
    lista fixa de "a defesa principal" era uma afirmacao que a medicao nao sustentava.
    """

    def test_alg_none_e_rejeitado(self, chaveiro: auth.Chaveiro) -> None:
        """Token sem assinatura, com `alg: none` no header.

        Uma implementacao que passe `algorithms=[header["alg"]]` — que e o que parece
        natural — aceita isto. O atacante escolhe o proprio verificador.
        """
        agora = int(time.time())
        forjado = jwt.encode(
            {
                "iss": emissor.EMISSOR_LOCAL,
                "sub": "atacante",
                "aud": AUDIENCIA,
                "iat": agora,
                "exp": agora + 3600,
                "scope": "analises:ler analises:escrever",
            },
            key="",
            algorithm="none",
        )

        with pytest.raises(auth.TokenInvalido):
            _verificar(forjado, chaveiro)

    def test_hs256_assinado_com_a_chave_publica_e_rejeitado(self, chaves: Path) -> None:
        """O ataque classico de confusao RS256/HS256.

        A chave publica **e publica**. Se o verificador aceitar `alg: HS256`, ele usa essa
        chave como segredo HMAC — e o atacante, que tambem a tem, consegue produzir uma
        assinatura que fecha. Assinatura valida, identidade escolhida por quem atacou.

        O JWT e montado a mao porque o PyJWT se **recusa** a produzir este token: o
        `HMACAlgorithm.prepare_key` levanta `InvalidKeyError` ao receber chave assimetrica.
        Medido depois: essa mesma guarda vale para quem **verifica** — foi o que fez este
        teste continuar passando com `HS256` na lista de algoritmos permitidos.
        """
        publica = emissor.chave_publica(chaves).encode()
        agora = int(time.time())

        def b64(dados: bytes) -> bytes:
            return base64.urlsafe_b64encode(dados).rstrip(b"=")

        cabecalho = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        corpo = b64(
            json.dumps(
                {
                    "iss": emissor.EMISSOR_LOCAL,
                    "sub": "atacante",
                    "aud": AUDIENCIA,
                    "iat": agora,
                    "exp": agora + 3600,
                    "scope": "analises:ler analises:escrever",
                }
            ).encode()
        )
        assinatura = b64(hmac.new(publica, cabecalho + b"." + corpo, hashlib.sha256).digest())
        forjado = (cabecalho + b"." + corpo + b"." + assinatura).decode()

        chaveiro = auth.Chaveiro.de_chave_publica(publica.decode())

        with pytest.raises(auth.TokenInvalido):
            _verificar(forjado, chaveiro)

    def test_a_lista_de_algoritmos_tem_um_unico_item(self) -> None:
        """O unico teste desta classe que **detecta** a mutacao.

        Os dois acima continuam verdes com `HS256` na lista, porque o PyJWT barra por outro
        caminho. Este falha, e a mensagem diz por que — que e o que alguem le antes de
        adicionar um algoritmo "para compatibilidade" com um IdP legado.
        """
        assert auth.ALGORITMOS == ("RS256",)

    def test_assinatura_nunca_e_dispensada(self) -> None:
        """A brecha que de fato aceita o token forjado.

        Medido: com `options={"verify_signature": False}`, o token de `alg: none` acima passa
        inteiro — `sub=atacante`, `scope=analises:ler`. Nenhuma lista de algoritmos protege
        disso, porque a verificacao nem acontece.

        E o caminho mais plausivel de todos: `verify_signature: False` e o que se escreve
        para "so ler as claims" num script de depuracao, e e uma linha que sobrevive a uma
        revisao distraida.

        A primeira versao deste teste lia o **codigo-fonte** do modulo com `in`, e falhou
        casando com o proprio comentario que documenta o ataque. Foi o que motivou extrair
        `OPCOES_DE_VERIFICACAO` para constante: configuracao que precisa de teste tem de ser
        inspecionavel em runtime, nao procurada como texto.
        """
        assert auth.VERIFICACOES, "nenhuma verificacao declarada"
        for chave, ativa in auth.VERIFICACOES.items():
            assert ativa is True, chave

    @pytest.mark.parametrize("obrigatoria", ["exp", "iat", "iss", "aud", "sub"])
    def test_claims_obrigatorias_estao_declaradas(self, obrigatoria: str) -> None:
        """`require` e o que impede token sem `exp` — que nunca expira."""
        assert obrigatoria in auth.CLAIMS_OBRIGATORIAS


class TestClaimsObrigatorias:
    def test_token_expirado(self, chaves: Path, chaveiro: auth.Chaveiro) -> None:
        """`agora` injetado em vez de dormir: teste dependente de relogio ja apareceu aqui."""
        antigo = emissor.emitir(
            audiencia=AUDIENCIA,
            escopos=["analises:ler"],
            diretorio=chaves,
            validade_segundos=60,
            agora=int(time.time()) - 3600,
        )

        with pytest.raises(auth.TokenExpirado) as exc:
            _verificar(antigo, chaveiro)

        assert exc.value.motivo == "expirado"

    def test_folga_de_relogio_aceita_token_recem_emitido(
        self, chaves: Path, chaveiro: auth.Chaveiro
    ) -> None:
        """Deriva de poucos segundos entre emissor e servico nao pode rejeitar.

        Sem `leeway`, isto e uma falha intermitente que so aparece quando emissor e servico
        estao em maquinas diferentes — ou seja, nunca no laboratorio e sempre em producao.
        """
        do_futuro = emissor.emitir(
            audiencia=AUDIENCIA,
            escopos=["analises:ler"],
            diretorio=chaves,
            agora=int(time.time()) + 10,
        )

        _verificar(do_futuro, chaveiro)

    def test_emissor_desconhecido(self, chaves: Path, chaveiro: auth.Chaveiro) -> None:
        """Mesma chave, outro emissor.

        O cenario real: um IdP compartilhado entre varios sistemas. Sem validar `iss`, um
        token legitimo de outro sistema da mesma organizacao vale aqui.
        """
        alheio = emissor.emitir(
            audiencia=AUDIENCIA,
            escopos=["analises:escrever"],
            diretorio=chaves,
            emissor="https://outro-sistema.invalid",
        )

        with pytest.raises(auth.TokenInvalido):
            _verificar(alheio, chaveiro)

    def test_audiencia_de_outro_servico(self, chaves: Path, chaveiro: auth.Chaveiro) -> None:
        """A escalada lateral que este projeto tem tres oportunidades de sofrer.

        Um token do `customer-support` — o servico que fala com o publico — consultando
        analise de credito. A NetworkPolicy impede o caminho de rede; a validacao de `aud`
        impede o caminho da credencial.
        """
        do_outro = emissor.emitir(
            audiencia=OUTRA_AUDIENCIA, escopos=["analises:ler"], diretorio=chaves
        )

        with pytest.raises(auth.AudienciaIncorreta):
            _verificar(do_outro, chaveiro)

    @pytest.mark.parametrize("ausente", ["exp", "iat", "iss", "aud", "sub"])
    def test_claim_obrigatoria_ausente(
        self, chaves: Path, chaveiro: auth.Chaveiro, ausente: str
    ) -> None:
        """`require` explicito, e cada claim tem consequencia propria se faltar.

        Sem `exp` o token **nunca expira**; sem `sub` nao ha o que registrar na trilha de
        auditoria; sem `aud` a validacao de audiencia nao tem o que comparar.
        """
        agora = int(time.time())
        conteudo = {
            "iss": emissor.EMISSOR_LOCAL,
            "sub": "cliente",
            "aud": AUDIENCIA,
            "iat": agora,
            "exp": agora + 3600,
            "scope": "analises:ler",
        }
        del conteudo[ausente]

        privada = (chaves / emissor.NOME_PRIVADA).read_text(encoding="utf-8")
        incompleto = jwt.encode(conteudo, privada, algorithm="RS256")

        with pytest.raises(auth.TokenInvalido):
            _verificar(incompleto, chaveiro)

    def test_assinatura_de_outra_chave(self, chaves: Path, tmp_path: Path) -> None:
        """Token bem formado, assinado por chave que este servico nao conhece."""
        outras = emissor.gerar_chaves(tmp_path / "outras")
        forjado = emissor.emitir(audiencia=AUDIENCIA, escopos=["analises:ler"], diretorio=outras)

        chaveiro = auth.Chaveiro.de_chave_publica(emissor.chave_publica(chaves))

        with pytest.raises(auth.TokenInvalido):
            _verificar(forjado, chaveiro)


class TestEscopos:
    def test_escopo_ausente_e_erro_distinto_do_token_invalido(
        self, token: str, chaveiro: auth.Chaveiro
    ) -> None:
        """A distincao que vira 403 em vez de 401 na borda HTTP.

        401 diz "suas credenciais nao servem, tente outras"; 403 diz "servem e nao bastam".
        Devolver 401 aqui manda um cliente correto reautenticar num laco que nunca resolve —
        e, pior, esconde de quem opera que o problema e de permissao, nao de credencial.
        """
        with pytest.raises(auth.EscopoInsuficiente) as exc:
            _verificar(token, chaveiro, escopos_exigidos=["documentos:enviar"])

        assert not isinstance(exc.value, auth.TokenInvalido)
        assert exc.value.motivo == "escopo_insuficiente"

    def test_todos_os_escopos_exigidos_sao_conferidos(
        self, token: str, chaveiro: auth.Chaveiro
    ) -> None:
        """Um presente e outro ausente: a conjuncao precisa reprovar."""
        with pytest.raises(auth.EscopoInsuficiente):
            _verificar(token, chaveiro, escopos_exigidos=["analises:ler", "documentos:enviar"])

    def test_scope_como_lista_e_aceito(self, chaves: Path, chaveiro: auth.Chaveiro) -> None:
        """Alguns IdPs emitem `scp` como array em vez de `scope` como string.

        Tratar apenas a string faria os escopos daquele emissor virarem conjunto vazio — e
        conjunto vazio nao falha de forma visivel, ele nega tudo. O sintoma parece
        configuracao de permissao errada no IdP, e a investigacao vai para o lugar errado.
        """
        agora = int(time.time())
        privada = (chaves / emissor.NOME_PRIVADA).read_text(encoding="utf-8")
        com_scp = jwt.encode(
            {
                "iss": emissor.EMISSOR_LOCAL,
                "sub": "cliente",
                "aud": AUDIENCIA,
                "iat": agora,
                "exp": agora + 3600,
                "scp": ["analises:ler", "triagens:executar"],
            },
            privada,
            algorithm="RS256",
        )

        identidade = _verificar(com_scp, chaveiro)

        assert identidade.escopos == {"analises:ler", "triagens:executar"}

    def test_exigir_nao_tem_variante_que_devolve_bool(self) -> None:
        """`tem()` existe para consulta; a decisao passa por `exigir()`, que levanta.

        Uma API que devolvesse bool na checagem de permissao permitiria
        `if identidade.tem(...)` sem `else` — negacao esquecida e a falha silenciosa mais
        comum de autorizacao.
        """
        identidade = auth.Identidade(sujeito="x", escopos=frozenset({"a"}))

        assert identidade.tem("a")
        assert not identidade.tem("b")
        with pytest.raises(auth.EscopoInsuficiente):
            identidade.exigir("b")


class TestChaveiro:
    def test_recusa_chave_privada(self, chaves: Path) -> None:
        """Guarda contra o erro que transformaria o resource server em emissor.

        Uma chave privada no verificador e a capacidade de **assinar**, nao de verificar. O
        erro seria plausivel: quem configura o servico tem as duas no mesmo diretorio.
        """
        privada = (chaves / emissor.NOME_PRIVADA).read_text(encoding="utf-8")

        with pytest.raises(ValueError, match="PRIVADA"):
            auth.Chaveiro.de_chave_publica(privada)

    def test_recusa_duas_fontes_de_chave(self) -> None:
        """Ambiguidade sobre qual chave manda e como se aceita token que devia ser negado."""
        with pytest.raises(ValueError, match="exatamente uma"):
            auth.Chaveiro(cliente_jwks=None, pem=None)


class TestCabecalho:
    @pytest.mark.parametrize(
        "valor",
        [
            None,
            "",
            "Bearer",
            "Bearer ",
            "Basic dXNlcjpzZW5oYQ==",
            "abc123",
        ],
    )
    def test_cabecalho_invalido_devolve_none(self, valor: str | None) -> None:
        assert auth.extrair_do_cabecalho(valor) is None

    @pytest.mark.parametrize("esquema", ["Bearer", "bearer", "BEARER", "BeArEr"])
    def test_esquema_e_case_insensitive(self, esquema: str) -> None:
        """A RFC 7235 define o esquema como case-insensitive.

        Um cliente que envie `bearer` minusculo receberia 401 sem explicacao possivel — e a
        investigacao desse 401 nao chega no `==` que o causou.
        """
        assert auth.extrair_do_cabecalho(f"{esquema} abc.def.ghi") == "abc.def.ghi"

    def test_token_ausente_tem_motivo_proprio(self, chaveiro: auth.Chaveiro) -> None:
        with pytest.raises(auth.TokenAusente) as exc:
            _verificar(None, chaveiro)

        assert exc.value.motivo == "ausente"


class TestObservadores:
    def test_aceite_e_negativa_sao_notificados(self, token: str, chaveiro: auth.Chaveiro) -> None:
        vistos: list[tuple[str, str]] = []
        auth.registrar_observador(lambda evento, motivo: vistos.append((evento, motivo)))

        _verificar(token, chaveiro)
        with pytest.raises(auth.TokenAusente):
            _verificar(None, chaveiro)

        assert ("aceito", "ok") in vistos
        assert ("negado", "ausente") in vistos

    def test_observador_que_falha_nao_derruba_a_validacao(
        self, token: str, chaveiro: auth.Chaveiro
    ) -> None:
        """Falha ao medir nao pode transformar requisicao legitima em erro.

        E o contrario tambem: nao pode fazer uma ilegitima passar. Por isso o `try` envolve
        apenas a notificacao, nunca a decisao.
        """

        def quebrado(evento: str, motivo: str) -> None:
            raise RuntimeError("metrica indisponivel")

        auth.registrar_observador(quebrado)

        identidade = _verificar(token, chaveiro)

        assert identidade.sujeito == "cliente-de-desenvolvimento"

    def test_observador_que_falha_nao_deixa_passar_token_invalido(
        self, chaveiro: auth.Chaveiro
    ) -> None:
        auth.registrar_observador(lambda evento, motivo: (_ for _ in ()).throw(RuntimeError()))

        with pytest.raises(auth.TokenAusente):
            _verificar(None, chaveiro)


class TestEmissorLocal:
    def test_nao_sobrescreve_chave_existente(self, tmp_path: Path) -> None:
        """Regenerar invalida todo token em circulacao, sem aviso."""
        destino = emissor.gerar_chaves(tmp_path / "k")
        antes = (destino / emissor.NOME_PRIVADA).read_bytes()

        emissor.gerar_chaves(destino)

        assert (destino / emissor.NOME_PRIVADA).read_bytes() == antes

    def test_sobrescreve_quando_pedido(self, tmp_path: Path) -> None:
        destino = emissor.gerar_chaves(tmp_path / "k")
        antes = (destino / emissor.NOME_PRIVADA).read_bytes()

        emissor.gerar_chaves(destino, sobrescrever=True)

        assert (destino / emissor.NOME_PRIVADA).read_bytes() != antes

    def test_emitir_sem_chave_diz_o_que_fazer(self, tmp_path: Path) -> None:
        """Mensagem de erro com o comando exato.

        `FileNotFoundError: .chaves/privada.pem` manda quem esta configurando procurar no
        codigo o que gera aquele arquivo.
        """
        with pytest.raises(FileNotFoundError, match="gerar-chaves"):
            emissor.emitir(audiencia=AUDIENCIA, escopos=[], diretorio=tmp_path / "vazio")

    def test_emissor_usa_tld_reservado(self) -> None:
        """`.invalid` (RFC 2606) nunca resolve.

        Um emissor com dominio plausivel convidaria alguem a apontar `jwks_uri` para ele.
        """
        assert emissor.EMISSOR_LOCAL.endswith(".invalid")
