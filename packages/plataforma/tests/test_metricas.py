"""Testes do rotulo de rota e das metricas HTTP compartilhadas.

O `rotulo_de_rota` e a razao pela qual `plataforma.metricas` existe. Ele e pequeno,
parece trivial, e **ja esteve errado uma vez** — usava `route.path`, que omite o prefixo
do `include_router`. Cada teste aqui trava um caso que custou uma investigacao.
"""

from __future__ import annotations

import pytest

from plataforma.metricas import (
    BUCKETS_HTTP,
    BUCKETS_INFERENCIA,
    ROTA_DESCONHECIDA,
    MetricasHTTP,
    criar_registro,
    rotulo_de_rota,
    template_de_rota,
)


class TestTemplateDeRota:
    def test_troca_o_valor_do_parametro_pelo_nome(self) -> None:
        assert (
            template_de_rota("/v1/analises/8c1f9e-abc/documentos", {"analise_id": "8c1f9e-abc"})
            == "/v1/analises/{analise_id}/documentos"
        )

    def test_preserva_o_prefixo_de_versao(self) -> None:
        """O bug original, e o motivo de a funcao reconstruir em vez de ler `route.path`.

        `route.path` devolveria `/analises/{analise_id}` sem o `/v1`, e no dia em que
        existisse um `/v2` as duas versoes cairiam na mesma serie temporal — o painel
        mostrando latencia somada, sem nada indicando a mistura.
        """
        rotulo = template_de_rota("/v1/analises/abc", {"analise_id": "abc"})

        assert rotulo.startswith("/v1/")

    def test_rota_sem_parametro_e_o_proprio_caminho(self) -> None:
        assert template_de_rota("/v1/analises", None) == "/v1/analises"
        assert template_de_rota("/health", {}) == "/health"

    def test_troca_por_segmento_inteiro_e_nao_por_substring(self) -> None:
        """O caso que uma implementacao com `str.replace` erraria em silencio.

        Aqui o valor do parametro (`abc`) tambem e o nome de um segmento fixo da rota.
        Troca por substring transformaria os dois, e o rotulo resultante nao
        corresponderia a rota alguma declarada na aplicacao.
        """
        rotulo = template_de_rota("/v1/abc/documentos/abc", {"doc_id": "abc"})

        assert rotulo == "/v1/{doc_id}/documentos/{doc_id}"

    def test_valor_numerico_e_convertido_antes_de_comparar(self) -> None:
        """`path_params` entrega o valor tipado; o caminho da URL e sempre texto."""
        assert template_de_rota("/v1/itens/42", {"item_id": 42}) == "/v1/itens/{item_id}"

    def test_valor_que_nao_aparece_no_caminho_nao_quebra(self) -> None:
        """Defensivo de proposito: rotulo errado e ruim, excecao no middleware e pior.

        Uma metrica com o caminho concreto polui uma serie temporal; uma excecao aqui
        derrubaria a requisicao **depois** de ela ter sido atendida com sucesso.
        """
        assert template_de_rota("/v1/itens/42", {"outro": "99"}) == "/v1/itens/42"


class TestDefesaContraVarredura:
    def test_rota_nao_casada_colapsa_num_rotulo_unico(self) -> None:
        """O unico caminho de cardinalidade que um atacante controla de fora.

        Sem esta guarda, `/wp-admin`, `/.env` e cada variacao tentada por um scanner
        viram uma serie temporal nova. Serie no Prometheus custa memoria **para
        sempre** — nao apenas enquanto o valor aparece —, entao a varredura infla a
        memoria do Prometheus sem autenticacao e sem tocar em nenhuma rota da aplicacao.
        """
        varridas = ["/wp-admin", "/.env", "/api/v2/usuarios", "/admin.php"]

        rotulos = {rotulo_de_rota(url, None, casou_com_rota=False) for url in varridas}

        assert rotulos == {ROTA_DESCONHECIDA}

    def test_o_caminho_pedido_nunca_entra_no_rotulo(self) -> None:
        assert "wp-admin" not in rotulo_de_rota("/wp-admin", None, casou_com_rota=False)

    def test_rota_casada_passa_pelo_template(self) -> None:
        assert (
            rotulo_de_rota("/v1/itens/42", {"item_id": "42"}, casou_com_rota=True)
            == "/v1/itens/{item_id}"
        )


class TestBuckets:
    def test_http_termina_em_dez_segundos(self) -> None:
        """Requisicao REST acima disso e incidente, nao ponto de distribuicao."""
        assert BUCKETS_HTTP[-1] == 10.0

    def test_inferencia_cobre_a_latencia_medida_em_cpu(self) -> None:
        """Os buckets padrao do Prometheus terminam em 10s, e a medicao aqui foi 148s.

        Com o teto padrao, **toda** chamada de modelo cairia em `+Inf`, e
        `histogram_quantile` sobre um unico bucket infinito devolve numero sem
        significado: o p95 apareceria no painel e estaria errado.
        """
        assert BUCKETS_INFERENCIA[-1] >= 148.0

    @pytest.mark.parametrize("buckets", [BUCKETS_HTTP, BUCKETS_INFERENCIA])
    def test_sao_crescentes(self, buckets: tuple[float, ...]) -> None:
        assert list(buckets) == sorted(buckets)


class TestMetricasHTTP:
    def test_prefixo_separa_os_servicos_no_mesmo_prometheus(self) -> None:
        """Os tres servicos convivem no mesmo Prometheus.

        Com nome unico, distinguir a latencia de dois deles dependeria do label de job,
        que muda conforme o alvo do scrape (container ou host) — a mesma serie
        apareceria sob dois nomes de job diferentes.
        """
        registro = criar_registro()
        MetricasHTTP(registro, "kyc")

        nomes = {familia.name for familia in registro.collect()}

        assert any(nome.startswith("kyc_http") for nome in nomes), nomes

    def test_registro_proprio_permite_criar_a_aplicacao_varias_vezes(self) -> None:
        """No `REGISTRY` global a segunda criacao levantaria `Duplicated timeseries`.

        E a suite de cada servico cria a aplicacao dezenas de vezes.
        """
        for _ in range(3):
            MetricasHTTP(criar_registro(), "suporte")

    def test_registrar_alimenta_contador_e_histograma(self) -> None:
        registro = criar_registro()
        http = MetricasHTTP(registro, "credito")

        http.registrar("POST", "/v1/analises", 201, 0.42)

        assert (
            registro.get_sample_value(
                "credito_http_requisicoes_total",
                {"metodo": "POST", "rota": "/v1/analises", "status": "201"},
            )
            == 1.0
        )
        assert (
            registro.get_sample_value(
                "credito_http_duracao_segundos_count",
                {"metodo": "POST", "rota": "/v1/analises"},
            )
            == 1.0
        )

    def test_info_publica_valor_um_com_os_metadados_no_label(self) -> None:
        """O padrao `_info` permite juntar versao a qualquer metrica em PromQL.

        E o que responde "a latencia piorou depois do deploy?" sem anotacao manual no
        dashboard. O valor e sempre 1 de proposito: o dado esta nos labels.
        """
        registro = criar_registro()
        http = MetricasHTTP(registro, "kyc")

        http.publicar_info("0.1.0", "local")

        amostra = registro.get_sample_value("kyc_info", {"versao": "0.1.0", "ambiente": "local"})

        assert amostra == 1.0
