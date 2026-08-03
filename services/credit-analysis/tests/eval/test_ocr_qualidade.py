"""Avaliacao de acuracia do OCR por nivel de degradacao.

Como no eval de retrieval, isto nao e teste unitario: e medicao versionada.
Usa documentos sinteticos com ground truth exato (`tests/apoio`) e aplica
degradacoes controladas — foto tremida, scanner ruidoso, documento torto, baixa
resolucao.

Exige o Tesseract instalado. Rodar com:

    pytest -m ocr

## Medicao de referencia (2026-07, Tesseract 5.4.0, por)

Holerite, 5 campos de ground truth:

    perfil              conf     qualidade       campos
    limpo              89,84     confiavel        5/5
    foto_tremida       88,09     confiavel        5/5
    documento_torto    88,44     confiavel        5/5
    pouca_luz          86,71     confiavel        5/5
    baixa_resolucao    87,78     confiavel        4/5   <- falso positivo
    scanner_ruidoso    61,50     revisao          3/5
    foto_ruim          59,96     rejeitada        1/5

Extrato, 24 lancamentos:

    limpo              83,90     revisao         23/24  <- falso negativo

## O que a medicao mostrou

**A confianca do Tesseract e um bom preditor, mas nao suficiente.** Dois casos
quebram um limiar global:

- *Falso positivo* — `baixa_resolucao` sai com 87,8% (acima do limiar de 85% da
  POL-002) e perde o CPF. Confianca alta nao garante campo extraido.
- *Falso negativo* — o extrato limpo, com 23 de 24 lancamentos lidos
  corretamente, sai com 83,9%. Tabela densa de numeros monoespacados recebe
  score por palavra mais baixo que prosa, entao uma extracao praticamente
  perfeita seria mandada para revisao humana.

E por isso que `MotorOCRComEscalonamento` decide por **suficiencia de campos** e
usa a confianca como sinal secundario.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal

import numpy as np
import pytest

from credit_analysis.domain.documento import QualidadeExtracao
from credit_analysis.domain.exceptions import DadosInsuficientes
from credit_analysis.domain.extrato import analisar_extrato
from credit_analysis.infrastructure.ocr.extracao import extrair_holerite, extrair_transacoes
from credit_analysis.infrastructure.ocr.tesseract import OCRTesseract, localizar_binario
from tests.apoio.documentos_sinteticos import PERFIS, Degradacao, ExtratoBancario, Holerite

pytestmark = [
    pytest.mark.ocr,
    pytest.mark.skipif(
        localizar_binario() is None,
        reason="Tesseract nao instalado; ver docstring de infrastructure/ocr/tesseract.py",
    ),
]

# Pisos de regressao, com folga sobre o medido para tolerar variacao de versao
# do Tesseract sem virar teste instavel.
CONFIANCA_MINIMA_LIMPO = 85.0
CAMPOS_MINIMOS_LIMPO = 5

# 20 de 24, e a folga foi recalibrada por medicao em **dois** ambientes.
#
# Estava 22, valor com folga de 1 sobre os 23 lidos no Windows — onde o gerador resolve a fonte para
# Arial. No Linux, com DejaVu, a mesma imagem produz texto diferente (166 palavras contra 188) e o
# resultado correto e 22 lidos com 2 rejeitados:
#
# - a primeira linha de salario tem o sufixo `C` lido como `€` e, sendo a primeira, nao tem saldo
#   anterior para resolver a direcao — rejeitar e o comportamento certo;
# - uma linha teve o ano corrompido (`2025` lido como `202`) e cai fora do periodo declarado.
#
# Ou seja: 22 com folga zero, num piso cujo comentario prometia folga. Piso sem folga nao e piso, e
# a origem do problema nao e o numero — e o **input variar por maquina**, que so se resolve
# versionando uma fonte no repositorio (~700KB) para os dois ambientes renderizarem igual.
#
# Enquanto isso nao acontecer, o piso tem que tolerar a pior renderizacao conhecida. A afirmacao que
# nao depende de fonte nenhuma esta em `test_nenhuma_linha_de_lancamento_desaparece`, e e ela que
# pega perda silenciosa.
LANCAMENTOS_MINIMOS_LIMPO = 20  # de 24

PERFIS_POR_NOME = {p.nome: p for p in PERFIS}


@pytest.fixture(scope="module")
def motor() -> OCRTesseract:
    return OCRTesseract()


@pytest.fixture(scope="module")
def holerite() -> Holerite:
    return Holerite()


@pytest.fixture(scope="module")
def extrato() -> ExtratoBancario:
    return ExtratoBancario()


def campos_esperados(h: Holerite) -> dict[str, str]:
    return {
        "cpf": h.cpf.replace(".", "").replace("-", ""),
        "competencia": h.competencia,
        "salario_base": "8.500,00",
        "salario_liquido": "7.262,14",
        "empregador": "INDUSTRIA",
    }


async def extrair(motor: OCRTesseract, imagem: object) -> object:
    return await motor.extrair(np.asarray(imagem))


class TestHoleriteLimpo:
    async def test_extrai_todos_os_campos(self, motor: OCRTesseract, holerite: Holerite) -> None:
        ocr = await motor.extrair(np.asarray(holerite.renderizar()))
        extracao = extrair_holerite(ocr)

        assert float(ocr.confianca.valor) >= CONFIANCA_MINIMA_LIMPO
        assert ocr.qualidade is QualidadeExtracao.CONFIAVEL
        assert extracao.completa
        assert extracao.campos_nao_reconhecidos == ()

    async def test_renda_bate_com_o_ground_truth(
        self, motor: OCRTesseract, holerite: Holerite
    ) -> None:
        ocr = await motor.extrair(np.asarray(holerite.renderizar()))
        extracao = extrair_holerite(ocr)

        assert extracao.renda_comprovada is not None
        assert extracao.renda_comprovada.valor == holerite.salario_liquido

    async def test_cpf_bate_com_o_ground_truth(
        self, motor: OCRTesseract, holerite: Holerite
    ) -> None:
        ocr = await motor.extrair(np.asarray(holerite.renderizar()))
        extracao = extrair_holerite(ocr)

        assert extracao.cpf is not None
        assert extracao.cpf.valor_bruto == holerite.cpf.replace(".", "").replace("-", "")


class TestDegradacoes:
    @pytest.mark.parametrize(
        "nome_perfil", ["foto_tremida", "documento_torto", "pouca_luz", "baixa_resolucao"]
    )
    async def test_degradacao_leve_mantem_a_renda(
        self, motor: OCRTesseract, holerite: Holerite, nome_perfil: str
    ) -> None:
        """Perfis que o pre-processamento consegue compensar.

        `baixa_resolucao` esta aqui e perde o CPF, mas mantem a renda — que e o
        campo que alimenta o score.
        """
        perfil = PERFIS_POR_NOME[nome_perfil]
        ocr = await motor.extrair(np.asarray(perfil.aplicar(holerite.renderizar())))
        extracao = extrair_holerite(ocr)

        assert extracao.renda_comprovada is not None, f"perdeu a renda em {nome_perfil}"
        assert extracao.renda_comprovada.valor == holerite.salario_liquido

    @pytest.mark.parametrize("nome_perfil", ["scanner_ruidoso", "foto_ruim"])
    async def test_degradacao_severa_e_sinalizada(
        self, motor: OCRTesseract, holerite: Holerite, nome_perfil: str
    ) -> None:
        """Nao exigimos que o OCR acerte — exigimos que ele *avise* que errou.

        Extracao incompleta com confianca alta seria o pior resultado possivel:
        o caso passaria pela esteira sem revisao com dado faltando.
        """
        perfil = PERFIS_POR_NOME[nome_perfil]
        ocr = await motor.extrair(np.asarray(perfil.aplicar(holerite.renderizar())))
        extracao = extrair_holerite(ocr)

        degradou = ocr.qualidade is not QualidadeExtracao.CONFIAVEL
        incompleta = not extracao.completa
        assert degradou or not incompleta, (
            f"{nome_perfil}: extracao incompleta ({extracao.campos_nao_reconhecidos}) "
            f"com qualidade {ocr.qualidade.value} — passaria sem revisao"
        )

    async def test_rotacao_e_corrigida(self, motor: OCRTesseract, holerite: Holerite) -> None:
        perfil = PERFIS_POR_NOME["documento_torto"]
        ocr = await motor.extrair(np.asarray(perfil.aplicar(holerite.renderizar())))

        assert any("rotacao corrigida" in c for c in ocr.correcoes_aplicadas)

    async def test_imagem_pequena_e_ampliada(self, motor: OCRTesseract, holerite: Holerite) -> None:
        perfil = PERFIS_POR_NOME["baixa_resolucao"]
        ocr = await motor.extrair(np.asarray(perfil.aplicar(holerite.renderizar())))

        assert any("ampliada" in c for c in ocr.correcoes_aplicadas)


class TestExtratoBancario:
    async def test_le_quase_todos_os_lancamentos(
        self, motor: OCRTesseract, extrato: ExtratoBancario
    ) -> None:
        ocr = await motor.extrair(np.asarray(extrato.renderizar()))
        transacoes, _ = extrair_transacoes(ocr)

        assert len(transacoes) >= LANCAMENTOS_MINIMOS_LIMPO

    async def test_nenhuma_linha_de_lancamento_desaparece(
        self, motor: OCRTesseract, extrato: ExtratoBancario
    ) -> None:
        """A assercao que **nao** depende da qualidade do OCR, e a que faltava.

        `len(transacoes) >= 22` mede acuracia, e acuracia varia com o ambiente: a fonte do gerador
        e resolvida pelo que existe no sistema (Arial no Windows, DejaVu no Linux), e as duas
        renderizacoes produzem texto diferente — 188 contra 166 palavras, medido.

        Esta mede outra coisa: **conservacao**. Cada linha de lancamento termina lida ou rejeitada,
        e as duas listas somadas tem que dar o total. Vale com qualquer fonte, qualquer versao de
        Tesseract e qualquer qualidade de imagem, porque nao e uma afirmacao sobre ler bem — e sobre
        nao perder em silencio.

        E era exatamente esta a falha original: 0 lidos e 0 rejeitados de 24, com `0 + 0 = 0`. O
        piso de acuracia pegou o sintoma; esta assercao nomeia a causa.
        """
        ocr = await motor.extrair(np.asarray(extrato.renderizar()))
        transacoes, rejeitadas = extrair_transacoes(ocr)

        assert len(transacoes) + len(rejeitadas) == len(extrato.lancamentos)

    async def test_renda_mediana_bate_com_o_salario(
        self, motor: OCRTesseract, extrato: ExtratoBancario
    ) -> None:
        """A mediana da POL-005 secao 3 sobrevive a lancamento perdido.

        E exatamente por isso que a politica manda usar mediana e nao media: um
        mes lido pela metade nao distorce a renda apurada.
        """
        ocr = await motor.extrair(np.asarray(extrato.renderizar()))
        transacoes, _ = extrair_transacoes(ocr)
        resumo = analisar_extrato(transacoes)

        assert resumo.renda_mediana_mensal.valor == extrato.salario
        assert resumo.meses_analisados == extrato.meses

    async def test_nunca_infla_a_renda(self, motor: OCRTesseract, extrato: ExtratoBancario) -> None:
        """A propriedade fail-safe mais importante desta camada.

        Erro de OCR pode fazer a esteira aprovar credito que nao deveria se
        inflar a renda; se subestimar, o pior caso e uma negativa injusta que o
        cliente contesta. Nenhum perfil de degradacao pode produzir renda
        **acima** do ground truth.
        """
        for perfil in PERFIS:
            ocr = await motor.extrair(np.asarray(perfil.aplicar(extrato.renderizar())))
            transacoes, _ = extrair_transacoes(ocr)

            try:
                resumo = analisar_extrato(transacoes)
            except DadosInsuficientes:
                continue  # recusou apurar — comportamento correto

            assert resumo.renda_mediana_mensal.valor <= extrato.salario + Decimal("0.01"), (
                f"{perfil.nome} inflou a renda: {resumo.renda_mediana_mensal} "
                f"acima do ground truth {extrato.salario}"
            )

    async def test_extrato_ilegivel_recusa_apurar(self, motor: OCRTesseract) -> None:
        """Renda mediana zero e ausencia de informacao, nao renda de zero."""
        from credit_analysis.domain.documento import ResultadoOCR
        from credit_analysis.domain.value_objects import Percentual

        # Extrato com credito em apenas um dos quatro meses.
        texto = "\n".join(
            [
                "DATA HISTORICO VALOR SALDO",
                "05/01/2025 CREDITO SALARIO 8.000,00 C 8.000,00",
                "05/02/2025 PAGAMENTO 1.000,00 D 7.000,00",
                "05/03/2025 PAGAMENTO 1.000,00 D 6.000,00",
                "05/04/2025 PAGAMENTO 1.000,00 D 5.000,00",
            ]
        )
        transacoes, _ = extrair_transacoes(
            ResultadoOCR(texto=texto, confianca=Percentual.de(90), motor="teste")
        )

        with pytest.raises(DadosInsuficientes):
            analisar_extrato(transacoes)


class TestCorrelacaoConfiancaAcuracia:
    async def test_confianca_alta_implica_maioria_dos_campos(
        self, motor: OCRTesseract, holerite: Holerite
    ) -> None:
        """A confianca do Tesseract e um preditor util — com uma excecao medida.

        Todos os perfis classificados como CONFIAVEL extraem ao menos 4 dos 5
        campos. O piso e 4 e nao 5 justamente por causa do falso positivo do
        `baixa_resolucao`, documentado no cabecalho deste modulo.
        """
        esperados = campos_esperados(holerite)
        base = holerite.renderizar()

        for perfil in PERFIS:
            ocr = await motor.extrair(np.asarray(perfil.aplicar(base)))
            if ocr.qualidade is not QualidadeExtracao.CONFIAVEL:
                continue

            acertos = sum(1 for valor in esperados.values() if valor in ocr.texto)
            assert acertos >= 4, (
                f"{perfil.nome}: confianca {ocr.confianca} classificada como "
                f"CONFIAVEL mas so {acertos}/5 campos presentes"
            )


def _perfis_disponiveis() -> Iterator[Degradacao]:  # pragma: no cover - utilitario
    yield from PERFIS
