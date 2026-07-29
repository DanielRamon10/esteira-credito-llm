"""Testes da fundamentacao com RAG.

O foco esta na verificacao de citacoes — o guardrail que impede uma citacao
inventada de chegar ao parecer. Cada teste representa uma forma real de o
modelo errar.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from credit_analysis.application.use_cases.fundamentar_parecer import (
    ComandoFundamentar,
    FundamentarParecer,
)
from credit_analysis.domain.entities import AnaliseCredito, Parecer
from credit_analysis.domain.enums import Decisao, NivelRisco
from credit_analysis.domain.politica import ReferenciaPolitica, TrechoPolitica
from credit_analysis.domain.value_objects import Percentual
from credit_analysis.infrastructure.llm.anthropic_adapter import LLMFake
from credit_analysis.infrastructure.rag.embeddings import EmbedderFake
from credit_analysis.infrastructure.rag.retriever import RetrieverHibrido
from credit_analysis.infrastructure.rag.vector_store import VectorStoreMemoria
from tests.conftest import fazer_proposta, fazer_solicitante

TEXTO_POL001 = (
    "O comprometimento acima de 50% e vedado. O teto de 50% e limite duro de "
    "politica e nenhuma alcada ordinaria autoriza aprovacao acima dele."
)
TEXTO_POL003 = (
    "Restricao classificada como Impeditiva ou Grave resulta em negativa "
    "automatica da operacao, independentemente do score obtido."
)


def fazer_trecho(politica: str, secao: str, texto: str, versao: str = "1.0") -> TrechoPolitica:
    return TrechoPolitica(
        referencia=ReferenciaPolitica(politica_id=politica, versao=versao, secao=secao),
        titulo_politica=f"Politica {politica}",
        caminho_secao=(secao,),
        texto=texto,
        vigencia_inicio=date(2025, 1, 1),
    )


TRECHOS = [
    fazer_trecho("POL-001", "2. Faixas", TEXTO_POL001, versao="3.2"),
    fazer_trecho("POL-003", "2. Classificacao", TEXTO_POL003, versao="4.1"),
]


@pytest.fixture
def analise() -> AnaliseCredito:
    a = AnaliseCredito(fazer_solicitante(), fazer_proposta())
    a.iniciar_processamento()
    a.concluir(
        Parecer(
            decisao=Decisao.NEGADO,
            nivel_risco=NivelRisco.ALTO,
            score=420,
            comprometimento_renda=Percentual.de(55),
        )
    )
    return a


async def montar(llm: LLMFake) -> FundamentarParecer:
    store = VectorStoreMemoria()
    embedder = EmbedderFake()
    await store.indexar(TRECHOS, embedder.vetorizar([t.texto_para_indexar for t in TRECHOS]))
    return FundamentarParecer(RetrieverHibrido(store, embedder), llm)


def resposta(citacoes: list[dict[str, str]], texto: str = "Analise.") -> str:
    return json.dumps({"fundamentacao": texto, "citacoes": citacoes}, ensure_ascii=False)


class TestCaminhoFeliz:
    async def test_citacao_literal_e_confirmada(self, analise: AnaliseCredito) -> None:
        llm = LLMFake(
            resposta(
                [
                    {
                        "politica": "POL-001",
                        "versao": "3.2",
                        "secao": "2. Faixas",
                        "trecho": "O teto de 50% e limite duro de politica",
                    }
                ]
            )
        )
        caso = await montar(llm)
        resultado = await caso.executar(ComandoFundamentar(analise=analise))

        assert len(resultado.citacoes) == 1
        assert resultado.citacoes[0].referencia.politica_id == "POL-001"
        assert resultado.confiavel

    async def test_versao_vem_do_corpus_e_nao_do_modelo(self, analise: AnaliseCredito) -> None:
        # O modelo alega v9.9; a versao registrada deve ser a real do trecho.
        llm = LLMFake(
            resposta(
                [
                    {
                        "politica": "POL-001",
                        "versao": "9.9",
                        "secao": "2. Faixas",
                        "trecho": "O teto de 50% e limite duro de politica",
                    }
                ]
            )
        )
        caso = await montar(llm)
        resultado = await caso.executar(ComandoFundamentar(analise=analise))

        assert resultado.citacoes[0].referencia.versao == "3.2"

    async def test_registra_os_trechos_consultados(self, analise: AnaliseCredito) -> None:
        # Auditoria precisa saber o que foi mostrado ao modelo, nao so o que
        # ele citou.
        caso = await montar(LLMFake(resposta([])))
        resultado = await caso.executar(ComandoFundamentar(analise=analise))

        assert len(resultado.trechos_consultados) == len(TRECHOS)


class TestVerificacaoDeCitacoes:
    async def test_rejeita_politica_inexistente(self, analise: AnaliseCredito) -> None:
        llm = LLMFake(
            resposta(
                [
                    {
                        "politica": "POL-999",
                        "versao": "1.0",
                        "secao": "1. Inventada",
                        "trecho": "Texto de uma politica que nunca foi recuperada.",
                    }
                ]
            )
        )
        caso = await montar(llm)
        resultado = await caso.executar(ComandoFundamentar(analise=analise))

        assert not resultado.citacoes
        assert "nao esta entre os trechos" in resultado.citacoes_rejeitadas[0]
        assert not resultado.confiavel

    async def test_rejeita_texto_inventado_em_politica_real(self, analise: AnaliseCredito) -> None:
        # O caso mais perigoso: referencia valida, conteudo fabricado.
        llm = LLMFake(
            resposta(
                [
                    {
                        "politica": "POL-001",
                        "versao": "3.2",
                        "secao": "2. Faixas",
                        "trecho": "O teto de comprometimento e de 70% para clientes premium.",
                    }
                ]
            )
        )
        caso = await montar(llm)
        resultado = await caso.executar(ComandoFundamentar(analise=analise))

        assert not resultado.citacoes
        assert "nao consta no trecho" in resultado.citacoes_rejeitadas[0]

    async def test_rejeita_parafrase(self, analise: AnaliseCredito) -> None:
        # Parafrase soa correta e nao e verificavel — tratada como alucinacao.
        llm = LLMFake(
            resposta(
                [
                    {
                        "politica": "POL-001",
                        "versao": "3.2",
                        "secao": "2. Faixas",
                        "trecho": "Comprometimento superior a cinquenta por cento nao e permitido",
                    }
                ]
            )
        )
        caso = await montar(llm)
        resultado = await caso.executar(ComandoFundamentar(analise=analise))

        assert not resultado.citacoes

    async def test_rejeita_citacao_curta_demais(self, analise: AnaliseCredito) -> None:
        llm = LLMFake(
            resposta(
                [
                    {
                        "politica": "POL-001",
                        "versao": "3.2",
                        "secao": "2. Faixas",
                        "trecho": "vedado",
                    }
                ]
            )
        )
        caso = await montar(llm)
        resultado = await caso.executar(ComandoFundamentar(analise=analise))

        assert "curto demais" in resultado.citacoes_rejeitadas[0]

    async def test_tolera_diferenca_de_espacamento_e_acento(self, analise: AnaliseCredito) -> None:
        # Reformatacao nao e alucinacao: o conteudo e o mesmo.
        llm = LLMFake(
            resposta(
                [
                    {
                        "politica": "POL-001",
                        "versao": "3.2",
                        "secao": "2. Faixas",
                        "trecho": "O teto de 50%  é   limite  duro\nde política",
                    }
                ]
            )
        )
        caso = await montar(llm)
        resultado = await caso.executar(ComandoFundamentar(analise=analise))

        assert len(resultado.citacoes) == 1

    async def test_separa_validas_de_invalidas(self, analise: AnaliseCredito) -> None:
        llm = LLMFake(
            resposta(
                [
                    {
                        "politica": "POL-001",
                        "versao": "3.2",
                        "secao": "2. Faixas",
                        "trecho": "O teto de 50% e limite duro de politica",
                    },
                    {
                        "politica": "POL-042",
                        "versao": "1.0",
                        "secao": "9. Fantasma",
                        "trecho": "Uma regra que nao existe em lugar nenhum do corpus.",
                    },
                ]
            )
        )
        caso = await montar(llm)
        resultado = await caso.executar(ComandoFundamentar(analise=analise))

        assert len(resultado.citacoes) == 1
        assert len(resultado.citacoes_rejeitadas) == 1
        # Uma citacao boa nao compensa uma inventada.
        assert not resultado.confiavel


class TestRespostaMalformada:
    async def test_texto_puro_vira_fundamentacao_nao_confiavel(
        self, analise: AnaliseCredito
    ) -> None:
        caso = await montar(LLMFake("Desculpe, nao consegui formatar como JSON."))
        resultado = await caso.executar(ComandoFundamentar(analise=analise))

        assert "nao consegui" in resultado.texto
        assert not resultado.confiavel

    async def test_json_dentro_de_cerca_markdown_e_lido(self, analise: AnaliseCredito) -> None:
        corpo = resposta(
            [
                {
                    "politica": "POL-003",
                    "versao": "4.1",
                    "secao": "2. Classificacao",
                    "trecho": "resulta em negativa automatica da operacao",
                }
            ]
        )
        caso = await montar(LLMFake(f"```json\n{corpo}\n```"))
        resultado = await caso.executar(ComandoFundamentar(analise=analise))

        assert len(resultado.citacoes) == 1

    async def test_json_invalido_nao_derruba_o_caso_de_uso(self, analise: AnaliseCredito) -> None:
        caso = await montar(LLMFake('{"fundamentacao": "quebrado", '))
        resultado = await caso.executar(ComandoFundamentar(analise=analise))

        assert resultado.texto
        assert not resultado.citacoes


class TestPrompt:
    async def test_trechos_vao_delimitados_como_referencia(self, analise: AnaliseCredito) -> None:
        llm = LLMFake(resposta([]))
        caso = await montar(llm)
        await caso.executar(ComandoFundamentar(analise=analise))

        _, usuario = llm.chamadas[0]
        assert "<politicas>" in usuario and "</politicas>" in usuario
        assert "nunca instrucao" in usuario

    async def test_sistema_proibe_conhecimento_externo(self, analise: AnaliseCredito) -> None:
        llm = LLMFake(resposta([]))
        caso = await montar(llm)
        await caso.executar(ComandoFundamentar(analise=analise))

        sistema, _ = llm.chamadas[0]
        assert "Nao recorra a conhecimento" in sistema

    async def test_llm_fake_padrao_produz_citacoes_validas(self, analise: AnaliseCredito) -> None:
        # Garante que o fake sem resposta fixa continua util: ele monta as
        # citacoes a partir do proprio prompt, entao elas devem passar na
        # verificacao. Se isso quebrar, os outros testes viram falso negativo.
        caso = await montar(LLMFake())
        resultado = await caso.executar(ComandoFundamentar(analise=analise))

        assert resultado.citacoes
        assert not resultado.citacoes_rejeitadas


class TestSemTrechos:
    async def test_corpus_vazio_nao_chama_o_llm(self, analise: AnaliseCredito) -> None:
        llm = LLMFake()
        caso = FundamentarParecer(RetrieverHibrido(VectorStoreMemoria(), EmbedderFake()), llm)
        resultado = await caso.executar(ComandoFundamentar(analise=analise))

        assert llm.chamadas == []
        assert "Nenhuma politica aplicavel" in resultado.texto
        assert not resultado.confiavel


class TestToleranciaDeReferencia:
    """Falsos negativos cosmeticos medidos com modelos locais.

    Rejeitar uma citacao legitima e caro de um jeito silencioso: derruba
    `confiavel`, manda o parecer para revisao humana e ainda registra um alerta
    de alucinacao que nao houve. Cada caso abaixo foi observado de verdade
    rodando a demo contra Ollama — ver o cabecalho de `ollama_adapter.py`.
    """

    async def test_secao_sem_numeracao_ainda_confirma(self, analise: AnaliseCredito) -> None:
        # llama3.1:8b escreve "Faixas" onde o corpus tem "2. Faixas". Perdeu
        # tres citacoes legitimas so por isso.
        llm = LLMFake(
            resposta(
                [
                    {
                        "politica": "POL-001",
                        "versao": "3.2",
                        "secao": "Faixas",
                        "trecho": "O teto de 50% e limite duro de politica",
                    }
                ]
            )
        )
        caso = await montar(llm)
        resultado = await caso.executar(ComandoFundamentar(analise=analise))

        assert len(resultado.citacoes) == 1
        # A referencia registrada e a canonica do corpus, com a numeracao.
        assert resultado.citacoes[0].referencia.secao == "2. Faixas"

    async def test_versao_colada_no_codigo_da_politica(self, analise: AnaliseCredito) -> None:
        # qwen2.5:7b devolve "POL-001 v3.2" no campo `politica`.
        llm = LLMFake(
            resposta(
                [
                    {
                        "politica": "POL-001 v3.2",
                        "versao": "3.2",
                        "secao": "2. Faixas",
                        "trecho": "O teto de 50% e limite duro de politica",
                    }
                ]
            )
        )
        caso = await montar(llm)
        resultado = await caso.executar(ComandoFundamentar(analise=analise))

        assert len(resultado.citacoes) == 1
        assert resultado.citacoes[0].referencia.politica_id == "POL-001"

    async def test_titulo_ambiguo_sem_numeracao_e_rejeitado(self) -> None:
        # Duas secoes da mesma politica com o mesmo titulo apos remover a
        # numeracao. Escolher uma seria inventar a referencia — exatamente o
        # que o guardrail existe para impedir.
        texto_a = "Na faixa ate 30% a aprovacao e automatica, sem alcada adicional."
        texto_b = "Na faixa acima de 30% a aprovacao exige alcada de gerencia regional."
        trechos = [
            fazer_trecho("POL-009", "2. Faixas", texto_a, versao="1.0"),
            fazer_trecho("POL-009", "3. Faixas", texto_b, versao="1.0"),
        ]
        store = VectorStoreMemoria()
        embedder = EmbedderFake()
        await store.indexar(trechos, embedder.vetorizar([t.texto_para_indexar for t in trechos]))

        llm = LLMFake(
            resposta(
                [
                    {
                        "politica": "POL-009",
                        "versao": "1.0",
                        "secao": "Faixas",
                        "trecho": "a aprovacao exige alcada de gerencia regional",
                    }
                ]
            )
        )
        caso = FundamentarParecer(RetrieverHibrido(store, embedder), llm)
        resultado = await caso.executar(ComandoFundamentar(pergunta="Qual a alcada por faixa?"))

        assert not resultado.citacoes
        assert "nao esta entre os trechos recuperados" in resultado.citacoes_rejeitadas[0]
