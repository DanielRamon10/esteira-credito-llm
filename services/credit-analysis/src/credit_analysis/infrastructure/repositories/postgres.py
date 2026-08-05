"""Repositorio de analise em Postgres.

## O que ele destrava

O adapter em memoria bloqueava tres coisas ao mesmo tempo: trabalhador como processo separado
(cada processo veria o proprio dicionario), mais de uma replica da API, e durabilidade da propria
analise — que sumia no restart.

## Agregado inteiro por transacao, e os filhos sao reescritos

`salvar` grava a raiz e **apaga e reinsere** documentos e dados extraidos, tudo numa transacao.

Diferenciar o que mudou seria mais eficiente e exigiria o repositorio saber quais filhos sao
novos, quais sumiram e quais mudaram — ou seja, rastreamento de estado que hoje nao existe em
lugar nenhum do dominio. Na escala real (poucos documentos por analise), reescrever custa menos
que manter esse rastreamento correto.

O que **nao** e negociavel e a transacao unica: sem ela, um documento anexado poderia existir sem
o parecer que ele produziu, e a analise ficaria num estado que o dominio nao permite construir.

## Bloqueio otimista

`UPDATE ... WHERE versao = :esperada`. Zero linhas afetadas significa que outro processo gravou
entre a leitura e a escrita, e o repositorio levanta `ConflitoDeVersao`.

E o unico bug que a Camada 9 **introduz**: com repositorio compartilhado, a API pode anexar um
documento enquanto o trabalhador aplica a extracao de outro, e o ultimo a gravar apagaria o
trabalho do primeiro. Ninguem veria — nao ha erro, e o cliente so notaria que o documento sumiu.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import structlog
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

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
    StatusAnalise,
    TipoDocumento,
)
from credit_analysis.domain.value_objects import CPF, Dinheiro, Percentual

logger = structlog.get_logger(__name__)


class ConflitoDeVersao(RuntimeError):
    """Outro processo gravou a analise entre a leitura e a escrita.

    **Transitorio.** Recarregar e reaplicar resolve, e e por isso que o trabalhador nao a inclui
    na lista de erros permanentes: a proxima tentativa parte do estado atual.

    Nao e um erro do chamador nem do banco — e a deteccao funcionando. Sem ela, a gravacao teria
    passado apagando o trabalho de outro processo.
    """


class RepositorioAnalisesPostgres:
    """Adapter do port `RepositorioAnalises` sobre Postgres."""

    __slots__ = ("_pool",)

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def salvar(self, analise: AnaliseCredito) -> None:
        async with self._pool.connection() as conexao, conexao.transaction():
            existe = await self._gravar_raiz(conexao, analise)
            if existe:
                # Filhos apagados e reinseridos. Ver o cabecalho sobre por que nao ha diff.
                await conexao.execute(
                    "DELETE FROM dado_extraido WHERE analise_id = %s", (analise.id,)
                )
                await conexao.execute("DELETE FROM documento WHERE analise_id = %s", (analise.id,))

            await self._gravar_documentos(conexao, analise)
            await self._gravar_dados(conexao, analise)

    async def _gravar_raiz(self, conexao: AsyncConnection[Any], analise: AnaliseCredito) -> bool:
        """Devolve True quando era um UPDATE (a analise ja existia)."""
        parecer = analise.parecer
        campos = (
            analise.status.value,
            analise.erro,
            analise.reavaliacoes,
            analise.motivo_reavaliacao,
            analise.solicitante.nome,
            analise.solicitante.cpf.numero,
            analise.solicitante.data_nascimento,
            analise.solicitante.renda_mensal_declarada.valor,
            analise.proposta.valor_solicitado.valor,
            analise.proposta.prazo_meses,
            analise.proposta.taxa_juros_mensal.valor,
            parecer.decisao.value if parecer else None,
            parecer.nivel_risco.value if parecer else None,
            parecer.score if parecer else None,
            parecer.comprometimento_renda.valor if parecer else None,
            (parecer.limite_recomendado.valor if parecer and parecer.limite_recomendado else None),
            parecer.justificativas if parecer else [],
            parecer.politicas_aplicadas if parecer else [],
            analise.criada_em,
            analise.atualizada_em,
        )

        # `versao` nao vive na entidade: o dominio nao deve conhecer controle de concorrencia. Ela
        # e lida na busca e guardada aqui, no adapter, associada ao objeto.
        esperada = _VERSOES.get(analise.id)

        if esperada is None:
            await conexao.execute(
                """
                INSERT INTO analise (
                    id, versao, status, erro, reavaliacoes, motivo_reavaliacao,
                    solicitante_nome, solicitante_cpf, solicitante_nascimento, renda_declarada,
                    proposta_valor, proposta_prazo, proposta_taxa,
                    parecer_decisao, parecer_nivel_risco, parecer_score,
                    parecer_comprometimento, parecer_limite,
                    parecer_justificativas, parecer_politicas,
                    criada_em, atualizada_em
                ) VALUES (%s, 1, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (analise.id, *campos),
            )
            _VERSOES[analise.id] = 1
            return False

        resultado = await conexao.execute(
            """
            UPDATE analise SET
                versao = versao + 1,
                status = %s, erro = %s, reavaliacoes = %s, motivo_reavaliacao = %s,
                solicitante_nome = %s, solicitante_cpf = %s, solicitante_nascimento = %s,
                renda_declarada = %s,
                proposta_valor = %s, proposta_prazo = %s, proposta_taxa = %s,
                parecer_decisao = %s, parecer_nivel_risco = %s, parecer_score = %s,
                parecer_comprometimento = %s, parecer_limite = %s,
                parecer_justificativas = %s, parecer_politicas = %s,
                criada_em = %s, atualizada_em = %s
            WHERE id = %s AND versao = %s
            """,
            (*campos, analise.id, esperada),
        )

        if resultado.rowcount == 0:
            # Zero linhas: ou a analise sumiu, ou a versao mudou. As duas exigem recarregar, e a
            # distincao nao muda a acao do chamador.
            raise ConflitoDeVersao(
                f"analise {analise.id} foi alterada por outro processo "
                f"(versao esperada {esperada}). Recarregue e reaplique."
            )

        _VERSOES[analise.id] = esperada + 1
        return True

    @staticmethod
    async def _gravar_documentos(conexao: AsyncConnection[Any], analise: AnaliseCredito) -> None:
        for ordem, doc in enumerate(analise.documentos):
            await conexao.execute(
                """
                INSERT INTO documento (
                    id, analise_id, tipo, nome_arquivo, conteudo_hash, estado,
                    texto_extraido, confianca_ocr, motor_ocr, erro,
                    referencia_chave, referencia_versao,
                    injecao_suspeita, categorias_injecao, exige_revisao, renda_comprovada,
                    renda_origem, submetido_em, ordem
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          %s)
                """,
                (
                    doc.id,
                    analise.id,
                    doc.tipo.value,
                    doc.nome_arquivo,
                    doc.conteudo_hash,
                    doc.estado.value,
                    doc.texto_extraido,
                    doc.confianca_ocr.valor if doc.confianca_ocr else None,
                    doc.motor_ocr,
                    doc.erro,
                    doc.referencia.chave if doc.referencia else None,
                    doc.referencia.versao if doc.referencia else None,
                    doc.injecao_suspeita,
                    list(doc.categorias_injecao),
                    doc.exige_revisao_humana,
                    doc.renda_comprovada.valor if doc.renda_comprovada else None,
                    doc.renda_origem.value if doc.renda_origem else None,
                    doc.submetido_em,
                    ordem,
                ),
            )

    @staticmethod
    async def _gravar_dados(conexao: AsyncConnection[Any], analise: AnaliseCredito) -> None:
        for ordem, dado in enumerate(analise.dados_extraidos):
            await conexao.execute(
                """
                INSERT INTO dado_extraido (
                    analise_id, documento_id, campo, valor, origem, confianca, ordem
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    analise.id,
                    dado.documento_id,
                    dado.campo,
                    dado.valor,
                    dado.origem.value,
                    dado.confianca.valor,
                    ordem,
                ),
            )

    async def buscar_por_id(self, analise_id: UUID) -> AnaliseCredito | None:
        async with self._pool.connection() as conexao:
            conexao.row_factory = dict_row  # type: ignore[assignment]
            cursor = await conexao.execute("SELECT * FROM analise WHERE id = %s", (analise_id,))
            linha = await cursor.fetchone()
            if linha is None:
                return None
            return await self._montar(conexao, dict(linha))

    async def listar(self, limite: int = 50, offset: int = 0) -> list[AnaliseCredito]:
        async with self._pool.connection() as conexao:
            conexao.row_factory = dict_row  # type: ignore[assignment]
            cursor = await conexao.execute(
                # `id DESC` no desempate: o relogio do Windows tem resolucao de ~15ms, e duas
                # analises criadas em sequencia recebem o mesmo timestamp. Sem ele a paginacao
                # poderia repetir ou pular registro.
                "SELECT * FROM analise ORDER BY criada_em DESC, id DESC LIMIT %s OFFSET %s",
                (limite, offset),
            )
            linhas = await cursor.fetchall()
            return [await self._montar(conexao, dict(linha)) for linha in linhas]

    async def contar(self) -> int:
        async with self._pool.connection() as conexao:
            cursor = await conexao.execute("SELECT count(*) FROM analise")
            linha = await cursor.fetchone()
            return int(linha[0]) if linha else 0

    async def buscar_por_documento(self, documento_id: UUID) -> AnaliseCredito | None:
        """Lookup direto pelo documento, para o `GET /v1/documentos/{id}`.

        Fora do port `RepositorioAnalises` de proposito: e uma consulta de leitura especifica de
        uma rota, e nao uma operacao do agregado. Poe-la no port obrigaria o adapter em memoria a
        implementa-la tambem, para servir uma otimizacao que so o Postgres tem.

        O router usa quando o repositorio a oferece e cai na varredura quando nao — que e o que o
        adapter em memoria faz hoje, e e aceitavel no volume dele.
        """
        async with self._pool.connection() as conexao:
            conexao.row_factory = dict_row  # type: ignore[assignment]
            cursor = await conexao.execute(
                """
                SELECT a.* FROM analise a
                JOIN documento d ON d.analise_id = a.id
                WHERE d.id = %s
                """,
                (documento_id,),
            )
            linha = await cursor.fetchone()
            if linha is None:
                return None
            return await self._montar(conexao, dict(linha))

    async def _montar(self, conexao: AsyncConnection[Any], linha: dict[str, Any]) -> AnaliseCredito:
        analise_id: UUID = linha["id"]
        # Guarda a versao lida, para o `salvar` seguinte compara-la.
        _VERSOES[analise_id] = int(linha["versao"])

        cursor = await conexao.execute(
            "SELECT * FROM documento WHERE analise_id = %s ORDER BY ordem", (analise_id,)
        )
        documentos = [_montar_documento(dict(d)) for d in await cursor.fetchall()]

        cursor = await conexao.execute(
            "SELECT * FROM dado_extraido WHERE analise_id = %s ORDER BY ordem", (analise_id,)
        )
        dados = [_montar_dado(dict(d)) for d in await cursor.fetchall()]

        return AnaliseCredito(
            id=analise_id,
            solicitante=Solicitante(
                nome=linha["solicitante_nome"],
                cpf=CPF(linha["solicitante_cpf"]),
                data_nascimento=_como_datetime(linha["solicitante_nascimento"]),
                renda_mensal_declarada=Dinheiro(linha["renda_declarada"]),
            ),
            proposta=PropostaCredito(
                valor_solicitado=Dinheiro(linha["proposta_valor"]),
                prazo_meses=int(linha["proposta_prazo"]),
                taxa_juros_mensal=Percentual(linha["proposta_taxa"]),
            ),
            documentos=documentos,
            dados_extraidos=dados,
            parecer=_montar_parecer(linha),
            status=StatusAnalise(linha["status"]),
            erro=linha["erro"],
            reavaliacoes=int(linha["reavaliacoes"]),
            motivo_reavaliacao=linha["motivo_reavaliacao"],
            criada_em=_como_datetime(linha["criada_em"]),
            atualizada_em=_como_datetime(linha["atualizada_em"]),
        )


# Versoes lidas, por id de analise.
#
# ## Por que um dicionario de modulo, e o que ele custa
#
# A versao nao vive na entidade porque o dominio nao deve conhecer controle de concorrencia — um
# campo `versao` em `AnaliseCredito` apareceria na resposta da API e em todo teste de dominio, para
# resolver um problema de persistencia.
#
# O preco e este estado fora do objeto. Ele funciona porque o ciclo e sempre
# `buscar -> mutar -> salvar` no mesmo processo, e a chave e o id da analise.
#
# **A limitacao**: o dicionario cresce sem limite num processo de vida longa. Para o volume deste
# projeto e irrelevante (um UUID e um int por analise ja vista); num sistema com milhoes, a saida e
# a versao viajar no objeto retornado — um wrapper `Versionado[AnaliseCredito]` no port, que muda a
# assinatura de todos os casos de uso. Fica anotado, e nao feito por antecipacao.
_VERSOES: dict[UUID, int] = {}


def _montar_documento(linha: dict[str, Any]) -> DocumentoSubmetido:
    chave = linha["referencia_chave"]
    versao = linha["referencia_versao"]
    return DocumentoSubmetido(
        id=linha["id"],
        tipo=TipoDocumento(linha["tipo"]),
        nome_arquivo=linha["nome_arquivo"],
        conteudo_hash=linha["conteudo_hash"],
        estado=EstadoDocumento(linha["estado"]),
        texto_extraido=linha["texto_extraido"],
        confianca_ocr=(
            Percentual(linha["confianca_ocr"]) if linha["confianca_ocr"] is not None else None
        ),
        motor_ocr=linha["motor_ocr"],
        erro=linha["erro"],
        # As duas colunas juntas ou nenhuma: uma referencia com chave e sem versao nao e uma
        # referencia — e o estado que a Camada 8 existe para nao ter.
        referencia=Referencia(chave=chave, versao=versao) if chave and versao else None,
        injecao_suspeita=bool(linha["injecao_suspeita"]),
        categorias_injecao=tuple(linha["categorias_injecao"] or ()),
        exige_revisao_humana=bool(linha["exige_revisao"]),
        renda_origem=(
            OrigemDaRenda(linha["renda_origem"]) if linha["renda_origem"] is not None else None
        ),
        renda_comprovada=(
            Dinheiro(linha["renda_comprovada"]) if linha["renda_comprovada"] is not None else None
        ),
        submetido_em=_como_datetime(linha["submetido_em"]),
    )


def _montar_dado(linha: dict[str, Any]) -> DadoExtraido:
    return DadoExtraido(
        campo=linha["campo"],
        valor=linha["valor"],
        origem=OrigemDado(linha["origem"]),
        confianca=Percentual(linha["confianca"]),
        documento_id=linha["documento_id"],
    )


def _montar_parecer(linha: dict[str, Any]) -> Parecer | None:
    if linha["parecer_decisao"] is None:
        return None
    limite = linha["parecer_limite"]
    return Parecer(
        decisao=Decisao(linha["parecer_decisao"]),
        nivel_risco=NivelRisco(linha["parecer_nivel_risco"]),
        score=int(linha["parecer_score"]),
        comprometimento_renda=Percentual(linha["parecer_comprometimento"]),
        justificativas=list(linha["parecer_justificativas"] or []),
        politicas_aplicadas=list(linha["parecer_politicas"] or []),
        limite_recomendado=Dinheiro(limite) if limite is not None else None,
    )


def _como_datetime(valor: Any) -> datetime:
    """O psycopg ja devolve `datetime`; a funcao existe para o mypy e para o caso de `str`."""
    if isinstance(valor, datetime):
        return valor
    return datetime.fromisoformat(str(valor))
