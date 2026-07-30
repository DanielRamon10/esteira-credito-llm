"""Metricas do kyc-compliance.

O que estes testes protegem nao e "o endpoint responde 200" — e que as series existam
com os labels certos e a cardinalidade limitada. Metrica declarada e nunca incrementada
passa em qualquer teste de fumaca e nao aparece no painel: e a pior combinacao possivel,
porque o codigo **parece** instrumentado.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from plataforma import seguranca

from kyc_compliance.api.app import criar_app
from kyc_compliance.config import Settings
from kyc_compliance.domain.triagem import EntradaRestritiva
from kyc_compliance.infrastructure.listas import ListasEmMemoria
from tests.conftest import CPF_DA_PEP, CPF_LIMPO, montar_cliente

pytestmark = pytest.mark.integration


@pytest.fixture
def client(settings_teste: Settings, entradas: list[EntradaRestritiva]) -> Iterator[TestClient]:
    app = criar_app(settings=settings_teste, listas=ListasEmMemoria(entradas, "teste"))
    with montar_cliente(app) as c:
        yield c


def _series(exposicao: str, nome: str) -> dict[str, float]:
    """Extrai `{labels} -> valor` de uma metrica.

    Duas armadilhas evitadas aqui, as duas ja encontradas neste projeto:

    1. `_created` de cada counter carrega um **timestamp Unix**. Buscar por substring na
       exposicao inteira casa com ele, e foi assim que um teste da camada de
       observabilidade passou a depender do relogio. A comparacao aqui e por linha
       completa, com o nome ancorado.
    2. O `REGISTRO` e criado no import do modulo, logo e **global de processo**: os
       contadores acumulam ao longo da suite inteira. Nenhuma assercao neste arquivo
       compara valor absoluto — todas comparam **delta** em volta da acao medida.
    """
    encontrados: dict[str, float] = {}
    for linha in exposicao.splitlines():
        if linha.startswith("#") or "_created" in linha:
            continue
        casamento = re.fullmatch(rf"{re.escape(nome)}(\{{.*\}})? (\S+)", linha)
        if casamento:
            encontrados[casamento.group(1) or ""] = float(casamento.group(2))
    return encontrados


def _delta(antes: dict[str, float], depois: dict[str, float]) -> dict[str, float]:
    """O que mudou entre duas leituras, descartando o que ficou parado."""
    mudou = {
        labels: valor - antes.get(labels, 0.0)
        for labels, valor in depois.items()
        if valor - antes.get(labels, 0.0) > 0
    }
    return mudou


class TestExposicao:
    def test_metrics_esta_na_raiz_e_fora_do_openapi(self, client: TestClient) -> None:
        """Fora do `/v1` de proposito: `/metrics` na raiz e o que todo scrape assume.

        E fora do OpenAPI porque o consumidor e o Prometheus, nao um cliente da API —
        aparecer no contrato publico convidaria alguem a integrar com ele.
        """
        assert client.get("/metrics").status_code == 200
        assert "/metrics" not in client.get("/openapi.json").json()["paths"]

    @pytest.mark.parametrize("nome", ["kyc_info", "kyc_http_em_andamento"])
    def test_series_de_infraestrutura_existem(self, client: TestClient, nome: str) -> None:
        assert _series(client.get("/metrics").text, nome), f"{nome} ausente da exposicao"


class TestMetricaDeDominio:
    def test_triagem_incrementa_contador_com_decisao_e_risco(self, client: TestClient) -> None:
        """As duas dimensoes respondem perguntas diferentes de conformidade.

        `decisao` responde "quantos foram reprovados"; `nivel_risco` responde "o quanto a
        fila do analista esta pesada". Medidas separadas, nao daria para ver que os
        reprovados estao concentrados no risco critico — ou espalhados, que e um sinal
        bem diferente sobre o casamento de nomes.
        """
        antes = _series(client.get("/metrics").text, "kyc_triagens_total")
        client.post("/v1/triagens", json={"nome": "Beatriz Nogueira Prado", "cpf": CPF_LIMPO})
        depois = _series(client.get("/metrics").text, "kyc_triagens_total")

        mudou = _delta(antes, depois)
        assert len(mudou) == 1, mudou
        (labels,) = mudou
        assert 'decisao="aprovado"' in labels
        assert "nivel_risco=" in labels

    def test_correspondencia_e_contada_por_nivel(self, client: TestClient) -> None:
        """Um deslocamento de `forte` para `parcial` sem mudanca de codigo e um alerta.

        Significa lista com nomes mais longos ou mais abreviados que antes — e mais
        `parcial` e mais revisao manual, que e custo de gente.
        """
        antes = _series(client.get("/metrics").text, "kyc_correspondencias_total")
        client.post("/v1/triagens", json={"nome": "Maria Fernanda Souza", "cpf": CPF_DA_PEP})
        depois = _series(client.get("/metrics").text, "kyc_correspondencias_total")

        mudou = _delta(antes, depois)
        assert mudou, "correspondencia encontrada e nao medida"
        assert any("nivel=" in labels for labels in mudou), mudou

    def test_entradas_avaliadas_e_medida(self, client: TestClient) -> None:
        """O denominador de tudo: uma decisao sobre lista vazia nao vale nada.

        Serve para cruzar com `kyc_triagens_total{decisao="aprovado"}` — aprovacao em
        massa com o numero de entradas caindo e o cenario perigoso deste dominio.
        """
        antes = _series(client.get("/metrics").text, "kyc_entradas_avaliadas_sum")
        client.post("/v1/triagens", json={"nome": "Beatriz Nogueira Prado", "cpf": CPF_LIMPO})
        depois = _series(client.get("/metrics").text, "kyc_entradas_avaliadas_sum")

        assert _delta(antes, depois), "nenhuma entrada contabilizada"

    def test_duracao_usa_buckets_de_milissegundos(self, client: TestClient) -> None:
        """A triagem e comparacao em memoria; buckets de inferencia nao servem aqui.

        Com o bucket mais baixo contendo toda a massa, o histograma para de medir: ele
        confirma que a operacao e rapida e perde a resolucao para mostrar que piorou.
        """
        client.post("/v1/triagens", json={"nome": "Beatriz Nogueira Prado", "cpf": CPF_LIMPO})

        exposicao = client.get("/metrics").text
        limites = [
            float(le)
            for le in re.findall(r'kyc_triagem_duracao_segundos_bucket\{le="([\d.]+)"\}', exposicao)
        ]

        assert limites, "histograma de duracao ausente"
        assert max(limites) <= 1.0, f"bucket mais alto e {max(limites)}s numa operacao em memoria"


class TestCardinalidade:
    def test_rota_no_label_preserva_o_prefixo_de_versao(self, client: TestClient) -> None:
        """Regressao: `route.path` do FastAPI omite o prefixo do `include_router`.

        Sem o `/v1` no label, no dia em que existir um `/v2` as duas versoes cairiam na
        mesma serie temporal e a latencia apareceria somada, sem nada indicando a
        mistura. Este bug ja existiu no `credit-analysis` e foi o que justificou extrair
        `template_de_rota` para a plataforma.
        """
        client.post("/v1/triagens", json={"nome": "Beatriz Nogueira Prado", "cpf": CPF_LIMPO})

        series = _series(client.get("/metrics").text, "kyc_http_requisicoes_total")

        assert any('rota="/v1/triagens"' in labels for labels in series), series

    def test_id_de_triagem_nao_vira_serie_temporal(self, client: TestClient) -> None:
        """Serie temporal no Prometheus custa memoria **para sempre**.

        Nao apenas enquanto o valor aparece: um label por triagem cresce sem limite e
        derruba o Prometheus muito antes de derrubar este servico.
        """
        criada = client.post(
            "/v1/triagens", json={"nome": "Beatriz Nogueira Prado", "cpf": CPF_LIMPO}
        ).json()
        client.get(f"/v1/triagens/{criada['id']}")

        exposicao = client.get("/metrics").text

        assert criada["id"] not in exposicao
        assert 'rota="/v1/triagens/{triagem_id}"' in exposicao

    def test_nome_e_cpf_nunca_aparecem_na_exposicao(self, client: TestClient) -> None:
        """`/metrics` nao tem autenticacao dentro do cluster.

        Cardinalidade seria o primeiro argumento, mas aqui o problema e maior: nome e CPF
        como label transformariam a exposicao de metricas em vazamento de dado pessoal,
        legivel por qualquer coisa que alcance a porta.
        """
        client.post("/v1/triagens", json={"nome": "Beatriz Nogueira Prado", "cpf": CPF_LIMPO})

        exposicao = client.get("/metrics").text

        assert "Beatriz" not in exposicao
        assert CPF_LIMPO not in exposicao
        assert CPF_LIMPO.replace(".", "").replace("-", "") not in exposicao


class TestAusenciaDeliberada:
    """O que este servico **nao** mede, e por que a ausencia e uma decisao."""

    def test_nao_expoe_metrica_de_injecao(self) -> None:
        """Diferente do `credit-analysis` e do `customer-support`, e de proposito.

        O `kyc-compliance` nao processa conteudo nao confiavel: recebe nome e CPF
        validados na borda e compara contra lista propria. Um contador de injecao aqui
        ficaria permanentemente em zero — ruido no painel e, pior, sugere uma cobertura
        que nao existe. Quem olhasse a lista de metricas concluiria que o servico esta
        protegido contra algo que ele nunca chega a enfrentar.
        """
        from kyc_compliance.infrastructure import metricas

        assert not hasattr(metricas, "injecao_detectada")

    def test_nao_registra_observador_na_plataforma(
        self, settings_teste: Settings, entradas: list[EntradaRestritiva]
    ) -> None:
        """Contrapartida do teste acima, no lado do gancho.

        Sem esta verificacao, alguem poderia religar o observador e o contador voltaria
        a existir por caminho indireto. A contagem e comparada **antes e depois** em vez
        de contra zero: `limpar_observadores()` aqui apagaria o gancho de outro teste, e
        a guarda de idempotencia impediria o religamento — a suite seguinte passaria a
        medir zero injecao sem nada falhar.
        """
        antes = len(seguranca._observadores)
        for _ in range(3):
            criar_app(settings=settings_teste, listas=ListasEmMemoria(entradas, "teste"))

        assert len(seguranca._observadores) == antes
