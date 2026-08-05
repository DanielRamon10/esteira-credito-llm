"""Avaliacao de acuracia do OCR por nivel de degradacao.

Como no eval de retrieval, isto nao e teste unitario: e medicao versionada.
Usa documentos sinteticos com ground truth exato (`tests/apoio`) e aplica
degradacoes controladas — foto tremida, scanner ruidoso, documento torto, baixa
resolucao.

Exige o Tesseract instalado. Rodar com:

    pytest -m ocr

## Medicao de referencia (2026-08, imagem deterministica)

A tabela anterior (2026-07) media um documento que **mudava por maquina**: o gerador
resolvia a fonte pelo sistema, e Windows e Linux renderizavam imagens diferentes. Ver
o cabecalho de `tests/apoio/documentos_sinteticos.py`; a imagem agora e byte-a-byte
identica (sha256 `5f13e9cb...`), com fonte versionada e `layout_engine` fixo.

Com input fixo, as duas colunas abaixo isolam a **unica** variavel que sobrou: a
versao do engine.

Holerite, 5 campos de ground truth (contagem por `contar_campos`):

    perfil              Tesseract local      Tesseract 5.5.0 (CI)
    limpo              89,59 confiavel 5/5   93,01 confiavel 5/5
    foto_tremida       89,06 confiavel 5/5   94,41 confiavel 5/5
    documento_torto    90,49 confiavel 5/5   94,91 confiavel 5/5
    baixa_resolucao    90,04 confiavel 5/5   94,53 confiavel 5/5
    pouca_luz          88,97 confiavel 5/5   92,75 confiavel 5/5
    scanner_ruidoso    73,99 revisao    3/5  75,55 revisao    2/5
    foto_ruim          67,65 revisao    1/5  69,31 revisao    2/5

Extrato, 24 lancamentos:

    limpo              81,93 revisao  24/24  93,51 confiavel 23/24

A contagem de campos tambem foi corrigida junto: ela comparava o CPF sem pontuacao
contra um texto pontuado, entao o campo so contava quando o OCR **comia** os pontos —
metrica que premiava leitura pior, com teto real de 4/5. Ver `campos_esperados`.

## O que a medicao mostra

**A confianca do Tesseract e um bom preditor, mas nao suficiente.** Continua valendo,
e os casos mudaram de lugar:

- *Falso positivo* — **nao ha mais nenhum**. O `baixa_resolucao` ocupava esse papel e
  passou a ler 5/5, e o `pouca_luz` o ocupou por um tempo e tambem le 5/5 agora. As duas
  correcoes que produziram isso estao descritas abaixo.
- *Falso negativo* — o extrato limpo sai com 81,93% no engine local apesar de ler
  **24 de 24**. Tabela densa de numeros monoespacados recebe score por palavra mais
  baixo que prosa, e uma extracao perfeita iria para revisao humana. No 5.5.0 o mesmo
  documento da 93,51%, o que mostra que este falso negativo depende do engine.

Vale notar o que a coluna dupla revela sobre os perfis severos: `foto_ruim` le 1/5 num
engine e 2/5 no outro, e `scanner_ruidoso` 3/5 e 2/5. Piso numerico em degradacao severa
mediria a versao do Tesseract, nao o pipeline — e e por isso que os testes desses perfis
afirmam **sinalizacao** (`revisao_humana`) em vez de contagem.

E por isso que `MotorOCRComEscalonamento` decide por **suficiencia de campos** e
usa a confianca como sinal secundario.

## Os dois defeitos que esta medicao encontrou

**1. A coluna de data invadia a de historico.** `m + 150` fixos contra uma data que mede
~169px na fonte versionada. Sem espaco em branco entre as colunas, o Tesseract nao emitia
separacao e o parser de extrato descartava a linha inteira: 0 lancamentos de 24, com 0
rejeitados. Corrigido medindo a largura da coluna; e o que fez `baixa_resolucao` deixar de
perder campo.

**2. O padrao de valor nao tolerava espaco em volta da virgula.** Sob `pouca_luz` o
Tesseract escreve

    VALOR LIQUIDO A RECEBER R$ 7.262 , 14

O rotulo sai **perfeito** — o que ele quebra e o numero. O campo nao casava,
`renda_comprovada` caia para o salario base, e a renda apurada ficava em R$ 8.500,00 contra
R$ 7.262,14: **17% acima**, na direcao que aprova credito que nao deveria. Pior, `completa`
ficava True e `holerite_suficiente` dizia que o documento servia, entao **nada escalava**.

Aqui houve um erro de diagnostico que vale registrar: a primeira leitura desta medicao
concluiu que o **rotulo** estava corrompido e o valor intacto, e a correcao foi construida
em cima disso — sinalizar a origem da renda e mandar para revisao humana. So ao afirmar
`"7.262,14" in ocr.texto` num teste ficou claro que era o contrario. Ler o numero certo e
melhor que sinalizar o errado, e `_VALOR` passou a tolerar o espaco.

## A rede de seguranca ficou, e por que

A queda para o bruto e alcancavel sempre que o liquido for ilegivel **de verdade** — rotulo
apagado, coluna cortada na digitalizacao, holerite que so imprime o bruto. O que a correcao
de `_VALOR` removeu foi um gatilho por espaco, nao a classe:

- `ExtracaoHolerite.origem_da_renda` diz de qual campo o numero saiu, e e propriedade
  derivada da mesma condicao que escolhe o valor — as duas nao tem como divergir;
- `holerite_suficiente` passou a exigir o liquido: a cadeia tenta um motor melhor quando ha
  um, porque existe numero melhor na imagem;
- `ResultadoProcessamento.exige_revisao_humana` ganhou um quarto gatilho, e um caso apurado
  pelo bruto vai para o analista;
- `documento.renda_origem` e persistido e exposto na resposta de polling, para a pergunta de
  auditoria "a renda deste parecer era liquida?" nao exigir reprocessar a imagem.

Estes quatro sao exercitados com **texto fixo** em `tests/unit/test_extracao_ocr.py` e
`tests/integration/test_api_documentos.py`, e nao por degradacao de imagem: a decisao nao
depende de OCR, e nao deveria depender do engine instalado para ser testada.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal

import numpy as np
import pytest

from credit_analysis.domain.documento import QualidadeExtracao
from credit_analysis.domain.exceptions import DadosInsuficientes
from credit_analysis.domain.extrato import analisar_extrato
from credit_analysis.infrastructure.ocr.extracao import (
    extrair_holerite,
    extrair_transacoes,
)
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

# 20 de 24, e agora a folga significa uma coisa so.
#
# ## O que este piso media antes, e nao devia
#
# Estava 22, calibrado sobre os 23 lidos no Windows — onde o gerador resolvia a fonte do sistema
# para Arial. No Linux, com DejaVu, a **imagem era outra** (166 palavras contra 188), e o piso
# media duas coisas ao mesmo tempo: a qualidade do OCR e qual fonte o sistema tinha. Foi o que fez
# o CI falhar lendo 0 de 24 enquanto a maquina de desenvolvimento lia 23.
#
# Isso acabou: `documentos_sinteticos` versiona a fonte e fixa o `layout_engine`, e a imagem e
# byte-a-byte identica em qualquer maquina (sha256 `5f13e9cb...`, conferido no Windows e no
# container Debian). A mesma investigacao achou o defeito de layout que escondia tudo — a coluna
# de data invadia a de historico, e o Tesseract nao tinha espaco em branco onde separar.
#
# ## A folga que sobrou
#
# Com input fixo, a unica variacao restante e a **versao do Tesseract**, que e o que um piso de
# regressao deve tolerar. Medido com a mesma imagem: 24 lidos com o Tesseract do Windows, 23 com o
# 5.5.0 do container (a diferenca e a primeira linha de salario, cujo sufixo `C` sai como `€` e que,
# sendo a primeira, nao tem saldo anterior para resolver a direcao — rejeitar e o certo).
#
# 20 da espaco para um desvio de tres linhas entre versoes de engine. Cair abaixo disso e regressao
# de verdade, e nao ruido de ambiente.
#
# A afirmacao que nao depende nem de fonte nem de engine esta em
# `test_nenhuma_linha_de_lancamento_desaparece`, e e ela que pega perda silenciosa.
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


def campos_esperados(h: Holerite) -> dict[str, tuple[str, ...]]:
    """Formas aceitas de cada campo no texto bruto do OCR.

    ## Por que tuplas e nao strings

    A versao anterior comparava o CPF **sem** pontuacao (`52998224725`) contra um texto em que ele
    aparece pontuado — e o efeito era perverso: o campo so contava quando o Tesseract **comia** os
    pontos. Ou seja, a metrica premiava leitura pior, e o teto real era 4/5 e nao 5/5, apesar de a
    tabela de referencia registrar 5/5.

    Foi assim que passou: no render antigo o OCR comia a pontuacao do CPF; no render deterministico
    ele a le corretamente, o campo deixou de casar, e a contagem caiu sem que a leitura piorasse.

    Aceitar as duas formas mede o que a tabela diz medir: o campo esta legivel no texto.
    """
    return {
        "cpf": (h.cpf, h.cpf.replace(".", "").replace("-", "")),
        "competencia": (h.competencia,),
        "salario_base": ("8.500,00",),
        "salario_liquido": ("7.262,14",),
        "empregador": ("INDUSTRIA",),
    }


def contar_campos(esperados: dict[str, tuple[str, ...]], texto: str) -> int:
    return sum(1 for formas in esperados.values() if any(f in texto for f in formas))


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
        "nome_perfil", ["foto_tremida", "documento_torto", "baixa_resolucao", "pouca_luz"]
    )
    async def test_degradacao_leve_mantem_a_renda(
        self, motor: OCRTesseract, holerite: Holerite, nome_perfil: str
    ) -> None:
        """Perfis que o pre-processamento consegue compensar.

        `pouca_luz` saiu desta lista e **voltou**, e a ida e a volta valem mais que o teste: ele
        passou a apurar pelo salario bruto quando a imagem virou deterministica, e a causa nao era
        degradacao demais — era o padrao de valor nao tolerar o espaco que o OCR insere em volta da
        virgula (`7.262 , 14`). Ver `_VALOR` em `infrastructure/ocr/extracao.py`.
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

        O piso desceu de 4 para 3, e a razao nao e o teste ter ficado permissivo: a imagem medida
        mudou. Com a renderizacao deterministica, `baixa_resolucao` deixou de ser o falso positivo
        (le 5/5) e `pouca_luz` passou a ser, com 3 dos 5 campos deste conjunto esperado.

        O que o piso defende e que uma classificacao `CONFIAVEL` nao seja catastroficamente errada —
        se ela pudesse vir com 1/5, a confianca nao serviria nem como sinal secundario. O que ele
        **nao** defende e completude: e por isso que `MotorOCRComEscalonamento` decide por campos, e
        e o falso positivo do `pouca_luz` que mostra o custo de decidir por confianca.
        """
        esperados = campos_esperados(holerite)
        base = holerite.renderizar()

        for perfil in PERFIS:
            ocr = await motor.extrair(np.asarray(perfil.aplicar(base)))
            if ocr.qualidade is not QualidadeExtracao.CONFIAVEL:
                continue

            acertos = contar_campos(esperados, ocr.texto)
            assert acertos >= 4, (
                f"{perfil.nome}: confianca {ocr.confianca} classificada como "
                f"CONFIAVEL mas so {acertos}/5 campos presentes"
            )


def _perfis_disponiveis() -> Iterator[Degradacao]:  # pragma: no cover - utilitario
    yield from PERFIS
