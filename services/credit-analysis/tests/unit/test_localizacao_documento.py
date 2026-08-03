"""Como a rota de polling vai do documento para a analise.

## Por que estes testes existem separados dos da rota

Os dois caminhos — `JOIN` no Postgres e varredura em memoria — **produzem a mesma resposta**. Um
teste que sobe a API e afirma `200` com o corpo certo passa com qualquer um dos dois, e passaria
tambem se o caminho rapido nunca fosse escolhido.

Ou seja: o teste de rota nao consegue distinguir "otimizacao funcionando" de "otimizacao morta".
Estes testes distinguem, contando chamadas.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from structlog.testing import capture_logs

from credit_analysis.api.routers.documentos import TETO_DA_VARREDURA, _localizar
from credit_analysis.domain.armazenamento import EstadoDocumento
from credit_analysis.domain.entities import (
    AnaliseCredito,
    DocumentoSubmetido,
    PropostaCredito,
    Solicitante,
)
from credit_analysis.domain.enums import TipoDocumento
from credit_analysis.domain.exceptions import AnaliseNaoEncontrada
from credit_analysis.domain.value_objects import CPF, Dinheiro, Percentual


def montar_analise(documento_id: UUID | None = None) -> AnaliseCredito:
    analise = AnaliseCredito(
        solicitante=Solicitante(
            nome="Maria Teste",
            cpf=CPF("11144477735"),
            data_nascimento=datetime(1990, 5, 12, tzinfo=UTC),
            renda_mensal_declarada=Dinheiro.de("8000"),
        ),
        proposta=PropostaCredito(
            valor_solicitado=Dinheiro.de("20000"),
            prazo_meses=24,
            taxa_juros_mensal=Percentual.de("1.99"),
        ),
    )
    if documento_id is not None:
        analise.documentos.append(
            DocumentoSubmetido(
                id=documento_id,
                tipo=TipoDocumento.HOLERITE,
                nome_arquivo="holerite.png",
                conteudo_hash="a" * 64,
                estado=EstadoDocumento.RECEBIDO,
            )
        )
    return analise


class RepositorioBase:
    """So o suficiente para satisfazer `RepositorioAnalises`, contando as chamadas."""

    def __init__(self, analises: list[AnaliseCredito]) -> None:
        self._analises = analises
        self.chamadas_listar = 0
        self.chamadas_busca = 0

    async def salvar(self, analise: AnaliseCredito) -> None:
        self._analises.append(analise)

    async def buscar_por_id(self, analise_id: UUID) -> AnaliseCredito | None:
        return next((a for a in self._analises if a.id == analise_id), None)

    async def listar(self, limite: int = 50, offset: int = 0) -> list[AnaliseCredito]:
        self.chamadas_listar += 1
        return self._analises[offset : offset + limite]

    async def contar(self) -> int:
        return len(self._analises)


class RepositorioSemBusca(RepositorioBase):
    """Como o adapter em memoria: nao implementa `BuscaPorDocumento`."""


class RepositorioComBusca(RepositorioBase):
    """Como o Postgres: implementa a capacidade opcional."""

    async def buscar_por_documento(self, documento_id: UUID) -> AnaliseCredito | None:
        self.chamadas_busca += 1
        return next(
            (a for a in self._analises if any(d.id == documento_id for d in a.documentos)),
            None,
        )


class TestEscolhaDoCaminho:
    """O que separa otimizacao viva de otimizacao morta."""

    async def test_repositorio_com_busca_nao_varre(self) -> None:
        """A assercao que importa e `chamadas_listar == 0`.

        Sem ela, o `isinstance` poderia estar sempre falso — por um import trocado, por um metodo
        renomeado no adapter, por `runtime_checkable` esquecido no Protocol — e todo teste de rota
        continuaria verde enquanto a producao varresse a tabela inteira a cada polling.
        """
        documento_id = uuid4()
        # Ruido: mais analises do que a procurada, para a varredura ter o que percorrer.
        repositorio = RepositorioComBusca(
            [montar_analise(), montar_analise(documento_id), montar_analise()]
        )

        analise, documento = await _localizar(repositorio, documento_id)

        assert documento.id == documento_id
        assert documento in analise.documentos
        assert repositorio.chamadas_busca == 1
        assert repositorio.chamadas_listar == 0

    async def test_repositorio_sem_busca_varre_e_acha(self) -> None:
        """O caminho de tras precisa continuar funcionando: e o do repositorio em memoria."""
        documento_id = uuid4()
        repositorio = RepositorioSemBusca([montar_analise(), montar_analise(documento_id)])

        _, documento = await _localizar(repositorio, documento_id)

        assert documento.id == documento_id
        assert repositorio.chamadas_listar == 1

    @pytest.mark.parametrize(
        "repositorio_classe", [RepositorioComBusca, RepositorioSemBusca], ids=["com", "sem"]
    )
    async def test_documento_inexistente_e_404_nos_dois_caminhos(
        self, repositorio_classe: type[RepositorioBase]
    ) -> None:
        """Os dois caminhos precisam **concordar** no negativo, e nao so no positivo.

        E onde um `JOIN` errado se esconde: `LEFT JOIN` em vez de `JOIN` devolveria uma analise
        com `d.id` nulo, e o caminho rapido responderia 200 para documento que nao existe.
        """
        repositorio = repositorio_classe([montar_analise(uuid4())])

        with pytest.raises(AnaliseNaoEncontrada):
            await _localizar(repositorio, uuid4())


class TestTetoDaVarredura:
    """O 404 que pode ser mentira."""

    async def test_varredura_no_teto_avisa_que_o_404_pode_ser_falso(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Documento na analise 1001 existe e a rota diz 404.

        Nao ha como corrigir isso na varredura — paginar tudo teria o custo que a varredura ja tem,
        multiplicado. O que da para fazer e nao mentir em silencio: o log distingue "nao existe" de
        "nao procurei ate o fim", e e a unica pista que alguem teria ao investigar um cliente
        reclamando de upload perdido.

        `monkeypatch` do teto em vez de 1000 analises de verdade: o que se mede aqui e a condicao
        `>= teto`, e ela nao fica mais verdadeira com mil objetos do que com dois.
        """
        monkeypatch.setattr("credit_analysis.api.routers.documentos.TETO_DA_VARREDURA", 2)
        repositorio = RepositorioSemBusca([montar_analise(uuid4()), montar_analise(uuid4())])

        with capture_logs() as registros, pytest.raises(AnaliseNaoEncontrada):
            await _localizar(repositorio, uuid4())

        avisos = [r for r in registros if r["event"] == "documento.varredura_no_teto"]
        assert len(avisos) == 1
        assert avisos[0]["log_level"] == "warning"
        assert avisos[0]["teto"] == 2

    async def test_varredura_abaixo_do_teto_nao_avisa(self) -> None:
        """O aviso precisa ser raro para significar algo.

        Um log de aviso em todo 404 legitimo — cliente sondando documento que ele mesmo digitou
        errado — treinaria quem opera a ignorar exatamente a linha que importa.
        """
        repositorio = RepositorioSemBusca([montar_analise(uuid4())])

        with capture_logs() as registros, pytest.raises(AnaliseNaoEncontrada):
            await _localizar(repositorio, uuid4())

        assert [r for r in registros if r["event"] == "documento.varredura_no_teto"] == []

    async def test_o_limite_pedido_e_o_mesmo_teto_da_comparacao(self) -> None:
        """Um numero, nao dois iguais.

        Com `listar(limite=1000)` escrito na mao no `_varrer` e `TETO_DA_VARREDURA = 1000` ao lado,
        mudar so um deles nao quebra teste nenhum e o aviso deixa de disparar em silencio: pedindo
        500 linhas, `len(analises) >= 1000` nunca e verdade, e o caso que o log existe para cobrir
        volta a ser um 404 sem pista.

        Por isso este teste le o `limite` que chegou ao repositorio, em vez de comparar a constante
        consigo mesma.
        """
        limites: list[int] = []

        class RepositorioQueRegistraOLimite(RepositorioSemBusca):
            async def listar(self, limite: int = 50, offset: int = 0) -> list[AnaliseCredito]:
                limites.append(limite)
                return await super().listar(limite, offset)

        repositorio = RepositorioQueRegistraOLimite([montar_analise(uuid4())])

        with pytest.raises(AnaliseNaoEncontrada):
            await _localizar(repositorio, uuid4())

        assert limites == [TETO_DA_VARREDURA]
