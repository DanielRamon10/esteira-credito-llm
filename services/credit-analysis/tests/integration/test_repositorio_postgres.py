"""Repositorio de analise em Postgres.

## O teste que justifica a camada

`TestConcorrencia::test_gravacao_concorrente_nao_apaga_trabalho_alheio`. Ele reproduz o **unico bug
que o repositorio compartilhado introduz**, e que o adapter em memoria nao tinha porque nao havia
dois processos:

    1. API carrega a analise, anexa um segundo documento, grava
    2. trabalhador (que carregou antes) aplica a extracao do primeiro, grava
    3. sem bloqueio otimista, o passo 2 sobrescreve e o segundo documento desaparece

Nada falharia. Nao ha erro, nao ha log, e o cliente so notaria que o documento que ele enviou nao
esta la. E o tipo de perda que aparece semanas depois como "o sistema as vezes perde documento".

O teste tambem verifica o inverso — que o conflito e **transitorio**: recarregar e reaplicar
funciona, e e o que o trabalhador faz.

## O outro grupo que importa: precisao decimal

O dominio usa `Decimal` de proposito em dinheiro. Uma coluna `DOUBLE PRECISION` reintroduziria o
erro de arredondamento que ele existe para evitar, e o sintoma seria uma parcela de R$ 1.234,57
virando R$ 1.234,5699999. `TestPrecisao` grava valores escolhidos para expor isso.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from credit_analysis.application.ports import (
    BuscaPorDocumento,
    CicloDeVidaDoDado,
    RepositorioAnalises,
)
from credit_analysis.config import get_settings
from credit_analysis.domain.armazenamento import EstadoDocumento, Referencia
from credit_analysis.domain.documento import OrigemDaRenda
from credit_analysis.domain.entities import (
    AnaliseCredito,
    DadoExtraido,
    DocumentoSubmetido,
    Parecer,
    PropostaCredito,
    Solicitante,
)
from credit_analysis.domain.enums import (
    Decisao,
    NivelRisco,
    OrigemDado,
    TipoDocumento,
)
from credit_analysis.domain.value_objects import CPF, Dinheiro, Percentual
from credit_analysis.infrastructure.repositories.postgres import (
    _VERSOES,
    ConflitoDeVersao,
    RepositorioAnalisesPostgres,
)

SUFIXO_TESTE = "_test"


def _dsn_de_teste() -> str:
    """Mesma disciplina do `test_pgvector`: nunca o banco de desenvolvimento.

    Estes testes apagam linhas das tabelas de analise. Apontados para o banco de desenvolvimento,
    apagariam analise real — e contra um banco compartilhado, dado de outra pessoa.

    A origem passa pelo `Settings` e nao so por `os.getenv` porque o README manda criar um `.env`:
    lendo apenas o ambiente, estes testes pulariam em silencio numa maquina com o banco de pe.
    """
    bruto = os.getenv("CREDIT_POSTGRES_DSN_TEST") or get_settings().postgres_dsn.strip()
    if not bruto:
        return ""

    partes = urlsplit(bruto)
    banco = partes.path.lstrip("/")
    if not banco.endswith(SUFIXO_TESTE):
        banco += SUFIXO_TESTE
    return urlunsplit(partes._replace(path=f"/{banco}"))


DSN = _dsn_de_teste()
_BANCO_E_DE_TESTE = bool(DSN) and urlsplit(DSN).path.lstrip("/").endswith(SUFIXO_TESTE)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not DSN,
        reason="CREDIT_POSTGRES_DSN nao definido; suba o banco com `docker compose up -d`",
    ),
    pytest.mark.skipif(
        bool(DSN) and not _BANCO_E_DE_TESTE,
        reason=f"DSN nao aponta para um banco terminando em {SUFIXO_TESTE}",
    ),
]


@pytest.fixture
async def pool() -> AsyncIterator[AsyncConnectionPool]:
    p = AsyncConnectionPool(DSN, min_size=1, max_size=4, open=False)
    await p.open(wait=True, timeout=15)
    async with p.connection() as conexao:
        # `DELETE` e nao `TRUNCATE`: o cascade do TRUNCATE exigiria listar as filhas, e uma tabela
        # nova esquecida na lista deixaria lixo entre execucoes. O `ON DELETE CASCADE` da FK cuida
        # das filhas sozinho.
        await conexao.execute("DELETE FROM analise")
    _VERSOES.clear()
    yield p
    await p.close()


@pytest.fixture
def repositorio(pool: AsyncConnectionPool) -> RepositorioAnalisesPostgres:
    return RepositorioAnalisesPostgres(pool)


def fazer_analise(
    *,
    com_parecer: bool = True,
    com_documento: bool = False,
    renda: str = "8500.00",
) -> AnaliseCredito:
    analise = AnaliseCredito(
        solicitante=Solicitante(
            nome="Maria Oliveira Santos",
            cpf=CPF("52998224725"),
            data_nascimento=datetime(1990, 5, 14, tzinfo=UTC),
            renda_mensal_declarada=Dinheiro.de(renda),
        ),
        proposta=PropostaCredito(
            valor_solicitado=Dinheiro.de("45000.00"),
            prazo_meses=36,
            taxa_juros_mensal=Percentual.de("1.99"),
        ),
    )
    if com_parecer:
        analise.parecer = Parecer(
            decisao=Decisao.APROVADO,
            nivel_risco=NivelRisco.MEDIO,
            score=712,
            comprometimento_renda=Percentual.de("18.44"),
            justificativas=["renda comprovada acima do minimo", "sem restricao cadastral"],
            politicas_aplicadas=["POL-001", "POL-005"],
            limite_recomendado=Dinheiro.de("52000.00"),
        )
    if com_documento:
        doc = DocumentoSubmetido(
            tipo=TipoDocumento.HOLERITE,
            nome_arquivo="holerite.png",
            conteudo_hash="a" * 64,
            referencia=Referencia(chave="documentos/x/y/holerite.png", versao="v1"),
        )
        analise.documentos.append(doc)
        analise.dados_extraidos.append(
            DadoExtraido(
                campo="salario_liquido",
                valor="7262.14",
                origem=OrigemDado.OCR,
                confianca=Percentual.de("92.5"),
                documento_id=doc.id,
            )
        )
    return analise


class TestIdaEVolta:
    async def test_agregado_completo_sobrevive(
        self, repositorio: RepositorioAnalisesPostgres
    ) -> None:
        """O que o adapter em memoria nunca exercitou: serializacao de verdade.

        Guardar o objeto num dicionario nao prova que ele **cabe** no banco. Enum, array, `Decimal`,
        `datetime` com fuso e value object aninhado sao cinco oportunidades de perder informacao, e
        nenhuma delas aparece com um `dict`.
        """
        original = fazer_analise(com_documento=True)
        await repositorio.salvar(original)

        lida = await repositorio.buscar_por_id(original.id)

        assert lida is not None
        assert lida.id == original.id
        assert lida.status is original.status
        assert lida.solicitante.nome == original.solicitante.nome
        assert lida.solicitante.cpf.numero == original.solicitante.cpf.numero
        assert lida.proposta.prazo_meses == original.proposta.prazo_meses

        assert lida.parecer is not None
        assert lida.parecer.decisao is Decisao.APROVADO
        assert lida.parecer.nivel_risco is NivelRisco.MEDIO
        assert lida.parecer.score == 712
        # Arrays do Postgres: ordem e conteudo preservados.
        assert lida.parecer.justificativas == original.parecer.justificativas  # type: ignore[union-attr]
        assert lida.parecer.politicas_aplicadas == ["POL-001", "POL-005"]

    async def test_analise_sem_parecer(self, repositorio: RepositorioAnalisesPostgres) -> None:
        """Parecer ausente e um estado legitimo: a analise existe antes de ser avaliada.

        Com as colunas do parecer em NOT NULL, este caso seria impossivel de gravar — e o sintoma
        apareceria no primeiro POST, nao aqui.
        """
        analise = fazer_analise(com_parecer=False)
        await repositorio.salvar(analise)

        lida = await repositorio.buscar_por_id(analise.id)

        assert lida is not None
        assert lida.parecer is None

    async def test_documento_e_dado_extraido_com_procedencia(
        self, repositorio: RepositorioAnalisesPostgres
    ) -> None:
        original = fazer_analise(com_documento=True)
        await repositorio.salvar(original)

        lida = await repositorio.buscar_por_id(original.id)

        assert lida is not None
        assert len(lida.documentos) == 1
        doc = lida.documentos[0]
        assert doc.id == original.documentos[0].id
        assert doc.tipo is TipoDocumento.HOLERITE
        assert doc.estado is EstadoDocumento.RECEBIDO

        assert len(lida.dados_extraidos) == 1
        dado = lida.dados_extraidos[0]
        assert dado.origem is OrigemDado.OCR
        # A ligacao dado -> documento e o que sustenta "de onde veio a renda deste parecer".
        assert dado.documento_id == doc.id

    async def test_referencia_versionada_sobrevive(
        self, repositorio: RepositorioAnalisesPostgres
    ) -> None:
        """As duas colunas juntas, ou nenhuma.

        Uma referencia com chave e sem versao **nao e** uma referencia — e exatamente o estado que a
        Camada 8 existe para nao ter, porque a deduplicacao por versao viraria dedupe por chave.
        """
        original = fazer_analise(com_documento=True)
        await repositorio.salvar(original)

        lida = await repositorio.buscar_por_id(original.id)

        assert lida is not None
        referencia = lida.documentos[0].referencia
        assert referencia is not None
        assert referencia.chave == "documentos/x/y/holerite.png"
        assert referencia.versao == "v1"

    async def test_documento_sem_referencia_fica_none(
        self, repositorio: RepositorioAnalisesPostgres
    ) -> None:
        analise = fazer_analise()
        analise.documentos.append(
            DocumentoSubmetido(
                tipo=TipoDocumento.HOLERITE, nome_arquivo="x.png", conteudo_hash="b" * 64
            )
        )
        await repositorio.salvar(analise)

        lida = await repositorio.buscar_por_id(analise.id)

        assert lida is not None
        assert lida.documentos[0].referencia is None

    async def test_inexistente_devolve_none(self, repositorio: RepositorioAnalisesPostgres) -> None:
        assert await repositorio.buscar_por_id(uuid4()) is None


class TestPrecisao:
    async def test_dinheiro_nao_perde_centavo(
        self, repositorio: RepositorioAnalisesPostgres
    ) -> None:
        """`NUMERIC` e nao `DOUBLE PRECISION`, e o teste que separa os dois.

        O dominio usa `Decimal` de proposito. Com float, `renda` de 8500.10 volta como
        8500.099999999999 — e uma parcela calculada sobre isso erra centavos que o cliente ve na
        fatura.

        Os valores foram escolhidos por serem os que float representa mal.
        """
        analise = fazer_analise(renda="8500.10")
        await repositorio.salvar(analise)

        lida = await repositorio.buscar_por_id(analise.id)

        assert lida is not None
        assert lida.solicitante.renda_mensal_declarada.valor == Decimal("8500.10")
        assert isinstance(lida.solicitante.renda_mensal_declarada.valor, Decimal)

    async def test_percentual_preserva_duas_casas(
        self, repositorio: RepositorioAnalisesPostgres
    ) -> None:
        analise = fazer_analise()
        await repositorio.salvar(analise)

        lida = await repositorio.buscar_por_id(analise.id)

        assert lida is not None
        assert lida.parecer is not None
        assert lida.parecer.comprometimento_renda.valor == Decimal("18.44")

    async def test_quem_arredonda_a_taxa_e_o_dominio_e_nao_o_banco(
        self, repositorio: RepositorioAnalisesPostgres
    ) -> None:
        """Onde a precisao se perde, e por que a coluna acompanha o dominio.

        Escrevi este teste esperando que `NUMERIC(6,4)` preservasse 1,9925%, com o argumento de que
        arredondar para 1,99% muda o total de um contrato de 36 meses. O argumento continua valido;
        o teste estava errado.

        `Percentual.__post_init__` **quantiza em duas casas**. O terceiro digito nunca sai do
        dominio, entao a coluna com quatro prometia uma precisao que nada entrega — e coluna mais
        precisa que o dominio nao e folga, e convite: alguem gravaria 1,9925 acreditando que ficou
        guardado, e o valor voltaria 1,99 sem nada indicando onde se perdeu.

        O schema virou `(6,2)`. Se o negocio precisar de quatro casas, a mudanca comeca em
        `Percentual` — e este teste falha, apontando para o lugar certo.
        """
        analise = fazer_analise()
        analise.proposta = PropostaCredito(
            valor_solicitado=Dinheiro.de("45000.00"),
            prazo_meses=36,
            taxa_juros_mensal=Percentual(Decimal("1.9925")),
        )
        # Ja arredondado **antes** de chegar ao banco.
        assert analise.proposta.taxa_juros_mensal.valor == Decimal("1.99")

        await repositorio.salvar(analise)
        lida = await repositorio.buscar_por_id(analise.id)

        assert lida is not None
        assert lida.proposta.taxa_juros_mensal.valor == Decimal("1.99")


class TestAtualizacao:
    async def test_segunda_gravacao_atualiza_em_vez_de_duplicar(
        self, repositorio: RepositorioAnalisesPostgres
    ) -> None:
        analise = fazer_analise()
        await repositorio.salvar(analise)

        analise.erro = "algo aconteceu"
        await repositorio.salvar(analise)

        assert await repositorio.contar() == 1
        lida = await repositorio.buscar_por_id(analise.id)
        assert lida is not None
        assert lida.erro == "algo aconteceu"

    async def test_filhos_sao_substituidos_e_nao_acumulados(
        self, repositorio: RepositorioAnalisesPostgres
    ) -> None:
        """O bug que o `DELETE` antes do `INSERT` impede.

        Sem ele, cada `salvar` inseriria os documentos de novo: uma analise gravada tres vezes
        apareceria com o mesmo documento em triplicata, e a interpretacao somaria a renda tres
        vezes. E o modo de falha mais provavel de um repositorio que reescreve filhos.
        """
        analise = fazer_analise(com_documento=True)
        await repositorio.salvar(analise)
        await repositorio.salvar(analise)
        await repositorio.salvar(analise)

        lida = await repositorio.buscar_por_id(analise.id)

        assert lida is not None
        assert len(lida.documentos) == 1
        assert len(lida.dados_extraidos) == 1

    async def test_documento_novo_aparece_na_releitura(
        self, repositorio: RepositorioAnalisesPostgres
    ) -> None:
        analise = fazer_analise()
        await repositorio.salvar(analise)

        analise.documentos.append(
            DocumentoSubmetido(
                tipo=TipoDocumento.EXTRATO_BANCARIO,
                nome_arquivo="extrato.pdf",
                conteudo_hash="c" * 64,
            )
        )
        await repositorio.salvar(analise)

        lida = await repositorio.buscar_por_id(analise.id)
        assert lida is not None
        assert [d.tipo for d in lida.documentos] == [TipoDocumento.EXTRATO_BANCARIO]


class TestConcorrencia:
    """O bug que o repositorio compartilhado introduz, e a defesa contra ele."""

    async def test_gravacao_concorrente_nao_apaga_trabalho_alheio(
        self, repositorio: RepositorioAnalisesPostgres
    ) -> None:
        """A corrida entre a API e o trabalhador, reproduzida.

        Os dois carregam a mesma versao. A API anexa um documento e grava; o trabalhador, que
        carregou antes, tenta gravar o estado dele — que **nao tem** aquele documento.

        Sem o bloqueio otimista, a segunda gravacao passaria e o documento desapareceria. O
        `ConflitoDeVersao` e a deteccao funcionando, e o teste tambem confirma o que **nao**
        aconteceu: o dado da primeira gravacao continua la.
        """
        analise = fazer_analise()
        await repositorio.salvar(analise)

        # Duas leituras independentes, como dois processos fariam.
        visao_da_api = await repositorio.buscar_por_id(analise.id)
        visao_do_trabalhador = await repositorio.buscar_por_id(analise.id)
        assert visao_da_api is not None and visao_do_trabalhador is not None

        # A API anexa e grava primeiro.
        visao_da_api.documentos.append(
            DocumentoSubmetido(
                tipo=TipoDocumento.HOLERITE, nome_arquivo="novo.png", conteudo_hash="d" * 64
            )
        )
        await repositorio.salvar(visao_da_api)

        # O trabalhador tenta gravar a visao antiga.
        #
        # `_VERSOES` guarda a versao lida por id, e as duas leituras compartilham a entrada — o que
        # e fiel ao cenario real, onde cada processo tem o proprio dicionario com a versao que
        # **ele** leu. Aqui a entrada foi atualizada pela gravacao da API, entao a forcamos de volta
        # para simular o processo que nao viu aquela escrita.
        _VERSOES[analise.id] = 1
        visao_do_trabalhador.erro = "extracao aplicada"

        with pytest.raises(ConflitoDeVersao, match="alterada por outro processo"):
            await repositorio.salvar(visao_do_trabalhador)

        # O que importa: o documento da API **nao** foi apagado.
        final = await repositorio.buscar_por_id(analise.id)
        assert final is not None
        assert len(final.documentos) == 1
        assert final.documentos[0].nome_arquivo == "novo.png"
        assert final.erro is None, "a gravacao conflitante teve efeito parcial"

    async def test_conflito_e_transitorio_recarregar_resolve(
        self, repositorio: RepositorioAnalisesPostgres
    ) -> None:
        """Por que o trabalhador classifica o conflito como transitorio.

        Recarregar parte do estado atual, e a reaplicacao e idempotente. Sem esta propriedade, o
        conflito teria de ir para a DLQ — e um upload simultaneo a uma extracao mandaria o documento
        para revisao humana sem motivo.
        """
        analise = fazer_analise()
        await repositorio.salvar(analise)

        antiga = await repositorio.buscar_por_id(analise.id)
        assert antiga is not None
        analise.erro = "gravado por outro"
        await repositorio.salvar(analise)

        _VERSOES[analise.id] = 1
        with pytest.raises(ConflitoDeVersao):
            await repositorio.salvar(antiga)

        # Recarrega e aplica sobre o estado atual: passa.
        atual = await repositorio.buscar_por_id(analise.id)
        assert atual is not None
        atual.motivo_reavaliacao = "reaplicado"
        await repositorio.salvar(atual)

        final = await repositorio.buscar_por_id(analise.id)
        assert final is not None
        assert final.erro == "gravado por outro"
        assert final.motivo_reavaliacao == "reaplicado"


class TestListagem:
    async def test_ordena_da_mais_recente_para_a_mais_antiga(
        self, repositorio: RepositorioAnalisesPostgres
    ) -> None:
        """`ORDER BY criada_em DESC, id DESC`.

        O `id` no desempate nao e enfeite: o relogio do Windows tem resolucao de ~15ms, e analises
        criadas em sequencia recebem o mesmo `criada_em`. Sem ele a ordem entre elas seria
        indefinida, e a paginacao poderia repetir ou pular registro.
        """
        agora = datetime.now(UTC)
        ids = []
        for i in range(3):
            analise = fazer_analise()
            analise.criada_em = agora.replace(microsecond=i * 1000)
            await repositorio.salvar(analise)
            ids.append(analise.id)

        listadas = await repositorio.listar(limite=10)

        assert [a.id for a in listadas] == list(reversed(ids))

    async def test_paginacao_nao_repete_nem_pula(
        self, repositorio: RepositorioAnalisesPostgres
    ) -> None:
        """Com timestamps **identicos**, que e o caso que o desempate por id resolve."""
        mesmo_instante = datetime.now(UTC)
        for _ in range(5):
            analise = fazer_analise()
            analise.criada_em = mesmo_instante
            await repositorio.salvar(analise)

        primeira = await repositorio.listar(limite=2, offset=0)
        segunda = await repositorio.listar(limite=2, offset=2)
        terceira = await repositorio.listar(limite=2, offset=4)

        vistos = [a.id for a in (*primeira, *segunda, *terceira)]

        assert len(vistos) == 5
        assert len(set(vistos)) == 5, "a paginacao repetiu registro"

    async def test_contar(self, repositorio: RepositorioAnalisesPostgres) -> None:
        for _ in range(3):
            await repositorio.salvar(fazer_analise())

        assert await repositorio.contar() == 3


class TestBuscaPorDocumento:
    async def test_encontra_a_analise_pelo_documento(
        self, repositorio: RepositorioAnalisesPostgres
    ) -> None:
        """O lookup do `GET /v1/documentos/{id}`: um JOIN pelo indice `idx_documento_id`.

        A rota escolhe este caminho quando o repositorio oferece o metodo, e varre quando nao — o
        adapter em memoria nao oferece, de proposito: seria implementar uma otimizacao de Postgres
        num dicionario. Qual dos dois roda esta medido em `test_localizacao_documento.py`, contando
        chamadas; aqui se mede se a consulta devolve a analise certa.
        """
        analise = fazer_analise(com_documento=True)
        await repositorio.salvar(analise)
        documento_id = analise.documentos[0].id

        encontrada = await repositorio.buscar_por_documento(documento_id)

        assert encontrada is not None
        assert encontrada.id == analise.id

    async def test_documento_inexistente_devolve_none(
        self, repositorio: RepositorioAnalisesPostgres
    ) -> None:
        assert await repositorio.buscar_por_documento(uuid4()) is None

    def test_satisfaz_o_port_em_tempo_de_execucao(
        self, repositorio: RepositorioAnalisesPostgres
    ) -> None:
        """O `isinstance` do router precisa dar verdadeiro **nesta classe**.

        Os testes de unidade do `_localizar` usam fakes: eles provam que o router escolhe o caminho
        rapido para quem oferece a capacidade, e nao que este adapter oferece. Renomear o metodo
        aqui — ou tirar `runtime_checkable` do Protocol — deixaria os fakes passando e poria o
        Postgres na varredura, calado.
        """
        assert isinstance(repositorio, BuscaPorDocumento)
        assert isinstance(repositorio, RepositorioAnalises)


class TestOrigemDaRenda:
    """A distincao liquido/bruto tem que sobreviver ao restart.

    E o que faz a diferenca entre "a renda deste parecer era liquida?" ser respondivel por consulta
    ou exigir reprocessar uma imagem que pode nao existir mais.
    """

    async def test_origem_sobrevive_a_ida_e_volta(
        self, repositorio: RepositorioAnalisesPostgres
    ) -> None:
        analise = fazer_analise(com_documento=True)
        analise.documentos[0].renda_comprovada = Dinheiro.de("8500.00")
        analise.documentos[0].renda_origem = OrigemDaRenda.BASE
        analise.documentos[0].exige_revisao_humana = True
        await repositorio.salvar(analise)

        lida = await repositorio.buscar_por_id(analise.id)

        assert lida is not None
        assert lida.documentos[0].renda_origem is OrigemDaRenda.BASE
        assert lida.documentos[0].exige_revisao_humana is True

    async def test_extrato_grava_origem_nula(
        self, repositorio: RepositorioAnalisesPostgres
    ) -> None:
        """`NULL` e valido e significa "a distincao nao se aplica", nao "faltou gravar".

        Extrato bancario apura renda pela mediana dos creditos, e nao existe um bruto para
        confundir com ela. Sem este teste, um `NOT NULL DEFAULT 'liquido'` posto por descuido faria
        todo extrato afirmar que a renda dele e liquida — afirmacao sem sentido, e falsa por vir de
        um default.
        """
        analise = fazer_analise(com_documento=True)
        analise.documentos[0].tipo = TipoDocumento.EXTRATO_BANCARIO
        analise.documentos[0].renda_comprovada = Dinheiro.de("8032.14")
        analise.documentos[0].renda_origem = None
        await repositorio.salvar(analise)

        lida = await repositorio.buscar_por_id(analise.id)

        assert lida is not None
        assert lida.documentos[0].renda_origem is None
        assert lida.documentos[0].renda_comprovada is not None

    async def test_o_banco_recusa_origem_fora_do_dominio(
        self, pool: AsyncConnectionPool, repositorio: RepositorioAnalisesPostgres
    ) -> None:
        """O `CHECK` da coluna, e por que ele nao e redundante com o StrEnum.

        O enum protege o caminho da aplicacao. O banco tem outros: uma migracao, um script de
        correcao, um `UPDATE` manual num incidente. `renda_origem = 'bruto'` — sinonimo plausivel de
        `base` — passaria em qualquer um deles e o `OrigemDaRenda(...)` da leitura levantaria
        `ValueError` depois, na hora de montar o agregado, com um erro longe da causa.
        """
        analise = fazer_analise(com_documento=True)
        await repositorio.salvar(analise)

        # `pytest.raises` dentro do `async with`, e nao ao lado: ele nao e um gerenciador de
        # contexto assincrono, e combinar os dois num unico `async with` estoura com um TypeError
        # sobre protocolo — erro que nao tem relacao com o que o teste mede.
        async with pool.connection() as conexao:
            with pytest.raises(Exception, match="renda_origem"):
                await conexao.execute(
                    "UPDATE documento SET renda_origem = %s WHERE id = %s",
                    ("bruto", analise.documentos[0].id),
                )


class TestApagamentoDeIdentificacao:
    """LGPD art. 18 contra POL-006 secao 5, a tensao central da Camada 10.

    Apagar tudo atende o titular e destroi a trilha que a obrigacao legal exige. Conservar tudo
    protege a trilha e ignora o direito. Estes testes medem a saida escolhida: identificacao sai,
    registro da decisao fica.
    """

    async def test_identificacao_sai_e_decisao_fica(
        self, pool: AsyncConnectionPool, repositorio: RepositorioAnalisesPostgres
    ) -> None:
        analise = fazer_analise(com_documento=True)
        await repositorio.salvar(analise)
        agora = datetime(2026, 8, 4, tzinfo=UTC)

        apagou = await repositorio.apagar_identificacao(analise.id, "pedido_do_titular", agora)

        assert apagou
        assert await repositorio.buscar_por_id(analise.id) is None

        async with pool.connection() as conexao:
            cursor = await conexao.execute(
                """
                SELECT decisao, score, justificativas, faixa_valor, prazo_meses, motivo
                FROM decisao_retida WHERE analise_id = %s
                """,
                (analise.id,),
            )
            linha = await cursor.fetchone()

        assert linha is not None
        assert linha[0] == Decisao.APROVADO.value
        assert linha[1] == 712
        # A fundamentacao sobrevive: e o que responde ao titular sob o art. 20.
        assert "renda comprovada acima do minimo" in linha[2]
        # 45.000 cai na faixa 10k-50k. O valor exato **nao** e conservado.
        assert linha[3] == "10k_50k"
        assert linha[4] == 36
        assert linha[5] == "pedido_do_titular"

    async def test_nada_identificavel_sobra_na_tabela_conservada(
        self, pool: AsyncConnectionPool, repositorio: RepositorioAnalisesPostgres
    ) -> None:
        """A assercao que da sentido a tabela, e ela precisa ser sobre o **schema**.

        Conferir que uma linha especifica nao tem CPF passaria com uma coluna `cpf` vazia por acaso.
        O que importa e a tabela nao **ter onde** guardar identificador: se alguem acrescentar uma
        coluna `solicitante_cpf` amanha, este teste falha antes de a primeira linha ser gravada.
        """
        analise = fazer_analise(com_documento=True)
        await repositorio.salvar(analise)
        await repositorio.apagar_identificacao(
            analise.id, "prazo_vencido", datetime(2026, 8, 4, tzinfo=UTC)
        )

        async with pool.connection() as conexao:
            cursor = await conexao.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'decisao_retida'
                """
            )
            colunas = {linha[0] for linha in await cursor.fetchall()}

        proibidas = {
            "solicitante_cpf",
            "solicitante_nome",
            "solicitante_nascimento",
            "renda_declarada",
            "texto_extraido",
            "conteudo_hash",
            "proposta_valor",
        }
        vazadas = colunas & proibidas
        assert not vazadas, f"coluna identificavel em decisao_retida: {vazadas}"

    async def test_analise_sem_parecer_nao_deixa_registro(
        self, pool: AsyncConnectionPool, repositorio: RepositorioAnalisesPostgres
    ) -> None:
        """Caso nunca decidido nao tem decisao a conservar.

        Inserir uma linha com score zero criaria registro de uma decisao que nao houve — pior que
        nao ter registro, porque um relatorio de politica contaria como negativa uma analise que
        ninguem avaliou.
        """
        analise = fazer_analise(com_parecer=False)
        await repositorio.salvar(analise)

        apagou = await repositorio.apagar_identificacao(
            analise.id, "pedido_do_titular", datetime(2026, 8, 4, tzinfo=UTC)
        )

        assert apagou
        async with pool.connection() as conexao:
            cursor = await conexao.execute(
                "SELECT count(*) FROM decisao_retida WHERE analise_id = %s", (analise.id,)
            )
            linha = await cursor.fetchone()
        assert linha is not None and linha[0] == 0

    async def test_apagar_o_que_nao_existe_devolve_falso(
        self, repositorio: RepositorioAnalisesPostgres
    ) -> None:
        """Distingue "apaguei" de "nao havia", e a rota precisa disso para responder 404.

        Devolver True para id inexistente faria o cliente receber confirmacao de exclusao de algo
        que nunca existiu — num pedido de titular, uma resposta falsa sobre dado pessoal.
        """
        assert not await repositorio.apagar_identificacao(
            uuid4(), "pedido_do_titular", datetime(2026, 8, 4, tzinfo=UTC)
        )

    async def test_apagamento_e_idempotente(
        self, pool: AsyncConnectionPool, repositorio: RepositorioAnalisesPostgres
    ) -> None:
        """Pedido reenviado nao duplica registro nem estoura.

        `ON CONFLICT DO NOTHING` na chave primaria cobre o caso, e ele importa: pedido de titular
        chega por canal humano, e ser reenviado e normal.
        """
        analise = fazer_analise(com_documento=True)
        await repositorio.salvar(analise)
        agora = datetime(2026, 8, 4, tzinfo=UTC)

        assert await repositorio.apagar_identificacao(analise.id, "pedido_do_titular", agora)
        assert not await repositorio.apagar_identificacao(analise.id, "pedido_do_titular", agora)

        async with pool.connection() as conexao:
            cursor = await conexao.execute(
                "SELECT count(*) FROM decisao_retida WHERE analise_id = %s", (analise.id,)
            )
            linha = await cursor.fetchone()
        assert linha is not None and linha[0] == 1


class TestPurgaDeTextoDeOCR:
    async def test_purga_texto_de_analise_parada(
        self, pool: AsyncConnectionPool, repositorio: RepositorioAnalisesPostgres
    ) -> None:
        analise = fazer_analise(com_documento=True)
        documento = analise.documentos[0]
        # Confianca, motor e renda entram juntos com o texto em `concluir_extracao`. O teste precisa
        # deles preenchidos porque a assercao central e que a purga leva o texto **e deixa** tudo o
        # que sustenta o parecer.
        documento.texto_extraido = "HOLERITE\nCPF: 529.982.247-25\nLIQUIDO 7.262,14"
        documento.confianca_ocr = Percentual.de("94.12")
        documento.motor_ocr = "tesseract:por"
        documento.renda_comprovada = Dinheiro.de("7262.14")
        await repositorio.salvar(analise)

        # A analise foi mexida por ultimo em abril; o limite da purga e maio.
        async with pool.connection() as conexao:
            await conexao.execute(
                "UPDATE analise SET atualizada_em = %s WHERE id = %s",
                (datetime(2026, 4, 1, tzinfo=UTC), analise.id),
            )

        purgadas = await repositorio.purgar_texto_de_ocr(datetime(2026, 5, 1, tzinfo=UTC))

        assert purgadas == 1
        lida = await repositorio.buscar_por_id(analise.id)
        assert lida is not None
        assert lida.documentos[0].texto_extraido is None
        # O que sustenta o parecer sobrevive: sem isto a purga destruiria a auditoria.
        assert lida.documentos[0].confianca_ocr == Percentual.de("94.12")
        assert lida.documentos[0].motor_ocr == "tesseract:por"
        assert lida.documentos[0].renda_comprovada == Dinheiro.de("7262.14")
        assert lida.parecer is not None
        assert lida.parecer.score == 712

    async def test_nao_purga_analise_recente(
        self, repositorio: RepositorioAnalisesPostgres
    ) -> None:
        """O par negativo. Uma purga que leva tudo passaria no teste de cima.

        `atualizada_em` fica em `now()` ao salvar, entao um limite no passado nao deve alcancar.
        """
        analise = fazer_analise(com_documento=True)
        analise.documentos[0].texto_extraido = "HOLERITE ..."
        await repositorio.salvar(analise)

        purgadas = await repositorio.purgar_texto_de_ocr(datetime(2020, 1, 1, tzinfo=UTC))

        assert purgadas == 0
        lida = await repositorio.buscar_por_id(analise.id)
        assert lida is not None
        assert lida.documentos[0].texto_extraido is not None

    async def test_purga_e_idempotente(
        self, pool: AsyncConnectionPool, repositorio: RepositorioAnalisesPostgres
    ) -> None:
        """Rodar duas vezes nao conta a mesma linha duas vezes.

        O `WHERE texto_extraido IS NOT NULL` e o que garante isso. Sem ele a metrica do job diria
        que purgou milhares de linhas toda noite — numero que nao serviria para detectar nada.
        """
        analise = fazer_analise(com_documento=True)
        analise.documentos[0].texto_extraido = "HOLERITE ..."
        await repositorio.salvar(analise)
        async with pool.connection() as conexao:
            await conexao.execute(
                "UPDATE analise SET atualizada_em = %s WHERE id = %s",
                (datetime(2026, 4, 1, tzinfo=UTC), analise.id),
            )

        limite = datetime(2026, 5, 1, tzinfo=UTC)
        assert await repositorio.purgar_texto_de_ocr(limite) == 1
        assert await repositorio.purgar_texto_de_ocr(limite) == 0


class TestBuscaPorCPF:
    async def test_acha_todas_as_analises_do_titular(
        self, repositorio: RepositorioAnalisesPostgres
    ) -> None:
        """Um pedido de exclusao precisa alcancar **todas**, e nao a mais recente."""
        primeira = fazer_analise(com_documento=True)
        segunda = fazer_analise()
        await repositorio.salvar(primeira)
        await repositorio.salvar(segunda)

        de_outra_pessoa = fazer_analise()
        de_outra_pessoa.solicitante.cpf = CPF("11144477735")
        await repositorio.salvar(de_outra_pessoa)

        achadas = await repositorio.buscar_por_cpf(CPF("52998224725"))

        assert {a.id for a in achadas} == {primeira.id, segunda.id}

    async def test_cpf_sem_analise_devolve_lista_vazia(
        self, repositorio: RepositorioAnalisesPostgres
    ) -> None:
        assert await repositorio.buscar_por_cpf(CPF("11144477735")) == []

    def test_satisfaz_o_port_em_tempo_de_execucao(
        self, repositorio: RepositorioAnalisesPostgres
    ) -> None:
        """Mesmo raciocinio do `BuscaPorDocumento`: o `isinstance` da rota precisa ser True."""
        assert isinstance(repositorio, CicloDeVidaDoDado)


class TestLGPD:
    async def test_apagar_analise_leva_documento_e_dado(
        self, pool: AsyncConnectionPool, repositorio: RepositorioAnalisesPostgres
    ) -> None:
        """`ON DELETE CASCADE`, e por que ele nao e conveniencia.

        A LGPD art. 18 da ao titular o direito de exclusao. Sem o cascade, apagar a analise
        deixaria orfaos com o hash e a referencia do documento — ou seja, rastro de dado pessoal
        num sistema que informou ter apagado.
        """
        analise = fazer_analise(com_documento=True)
        await repositorio.salvar(analise)

        async with pool.connection() as conexao:
            await conexao.execute("DELETE FROM analise WHERE id = %s", (analise.id,))

            cursor = await conexao.execute(
                "SELECT count(*) FROM documento WHERE analise_id = %s", (analise.id,)
            )
            linha = await cursor.fetchone()
            assert linha is not None and linha[0] == 0

            cursor = await conexao.execute(
                "SELECT count(*) FROM dado_extraido WHERE analise_id = %s", (analise.id,)
            )
            linha = await cursor.fetchone()
            assert linha is not None and linha[0] == 0

    async def test_cpf_nao_tem_indice(self, pool: AsyncConnectionPool) -> None:
        """A ausencia e deliberada, e o teste existe para ela nao ser "corrigida".

        Nao ha consulta por CPF na API. Um indice ali convidaria a criar uma — e busca por CPF e o
        caminho por onde um vazamento deixa de ser um registro e vira uma lista.
        """
        async with pool.connection() as conexao:
            cursor = await conexao.execute(
                """
                SELECT count(*) FROM pg_indexes
                WHERE tablename = 'analise' AND indexdef LIKE '%solicitante_cpf%'
                """
            )
            linha = await cursor.fetchone()

        assert linha is not None and linha[0] == 0, (
            "indice em solicitante_cpf: ver o comentario no schema sobre por que ele nao existe"
        )
