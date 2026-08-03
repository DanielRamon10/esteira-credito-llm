"""Testes da extracao de campos e do escalonamento de OCR.

Trabalham sobre texto — nao sobre imagem — de proposito: o que se testa aqui e a
interpretacao do texto extraido, e injetar o texto direto torna cada caso de
falha reproduzivel. A acuracia do OCR sobre imagem real e medida em
`tests/eval/test_ocr_qualidade.py`.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import numpy as np
import pytest

from credit_analysis.domain.documento import ResultadoOCR
from credit_analysis.domain.value_objects import Percentual
from credit_analysis.infrastructure.ocr.escalonamento import MotorOCRComEscalonamento
from credit_analysis.infrastructure.ocr.extracao import (
    extrair_holerite,
    extrair_transacoes,
    holerite_suficiente,
)
from credit_analysis.infrastructure.ocr.vision import OCRFake

IMAGEM_QUALQUER = np.zeros((10, 10), dtype=np.uint8)

HOLERITE_OK = """\
RECIBO DE PAGAMENTO DE SALARIO
INDUSTRIA BRASILEIRA DE COMPONENTES LTDA
CNPJ: 12.345.678/0001-90
Nome: MARIA OLIVEIRA SANTOS  Competencia: 06/2025
CPF: 529.982.247-25  Admissao: 15/03/2019
SALARIO BASE 8.500,00
INSS 876,02
VALOR LIQUIDO A RECEBER R$ 7.262,14
"""


def ocr(texto: str, confianca: float = 90) -> ResultadoOCR:
    return ResultadoOCR(texto=texto, confianca=Percentual.de(confianca), motor="teste")


class TestExtracaoHolerite:
    def test_extrai_todos_os_campos(self) -> None:
        e = extrair_holerite(ocr(HOLERITE_OK))

        assert e.cpf is not None and e.cpf.valor_bruto == "52998224725"
        assert e.competencia is not None and e.competencia.valor_bruto == "06/2025"
        assert e.salario_base is not None
        assert e.salario_liquido is not None
        assert e.renda_comprovada is not None
        assert str(e.renda_comprovada) == "R$ 7.262,14"
        assert e.completa

    def test_empregador_vem_da_linha_anterior_ao_cnpj(self) -> None:
        e = extrair_holerite(ocr(HOLERITE_OK))
        assert e.empregador is not None
        assert "INDUSTRIA BRASILEIRA" in e.empregador.valor_bruto

    def test_campo_ausente_e_reportado(self) -> None:
        e = extrair_holerite(ocr("RECIBO\nNome: JOAO DA SILVA\n"))
        assert "salario_liquido" in e.campos_nao_reconhecidos
        assert not e.completa

    def test_cpf_com_digito_confundido_pelo_ocr_e_corrigido(self) -> None:
        # O OCR le 0 como O. O DV do CPF permite confirmar a correcao — e por
        # isso o CPF e o campo mais confiavel do documento.
        texto = HOLERITE_OK.replace("529.982.247-25", "S29.982.247-25")
        e = extrair_holerite(ocr(texto))

        assert e.cpf is not None
        assert e.cpf.valor_bruto == "52998224725"
        # Correcao reduz a confianca: houve suposicao no meio.
        assert e.cpf.confianca < Percentual.de(99)

    def test_cpf_irrecuperavel_e_descartado_em_vez_de_aceito_errado(self) -> None:
        texto = HOLERITE_OK.replace("529.982.247-25", "111.111.111-11")
        e = extrair_holerite(ocr(texto))

        assert e.cpf is None
        assert "cpf" in e.campos_nao_reconhecidos

    def test_trecho_de_origem_preserva_a_linha_do_documento(self) -> None:
        e = extrair_holerite(ocr(HOLERITE_OK))
        assert e.salario_liquido is not None
        assert "7.262,14" in e.salario_liquido.trecho_origem


EXTRATO_OK = """\
EXTRATO DE CONTA CORRENTE
DATA HISTORICO VALOR SALDO
05/01/2025 CREDITO SALARIO EMPRESA 8.032,14 C 11.232,14
10/01/2025 PAGAMENTO CARTAO CREDITO 2.311,54 D 8.920,60
15/01/2025 ALUGUEL IMOVEL RESIDENCIAL 2.100,00 D 6.820,60
SALDO FINAL: 6.820,60
"""


class TestExtracaoExtrato:
    def test_extrai_lancamentos_com_sinal_correto(self) -> None:
        transacoes, rejeitadas = extrair_transacoes(ocr(EXTRATO_OK))

        assert len(transacoes) == 3
        assert rejeitadas == []
        assert transacoes[0].valor.valor == Decimal("8032.14")
        assert transacoes[1].valor.valor == Decimal("-2311.54")
        assert transacoes[2].valor.valor == Decimal("-2100.00")

    def test_ignora_linha_de_resumo(self) -> None:
        # Somar "SALDO FINAL" como credito inflaria a renda apurada.
        transacoes, _ = extrair_transacoes(ocr(EXTRATO_OK))
        assert all("SALDO" not in t.descricao.upper() for t in transacoes)

    def test_le_a_coluna_de_valor_e_nao_a_de_saldo(self) -> None:
        transacoes, _ = extrair_transacoes(ocr(EXTRATO_OK))
        # 11.232,14 e o saldo; se ele fosse lido como valor, a renda triplicaria.
        assert all(abs(t.valor.valor) < Decimal("9000") for t in transacoes)

    def test_valor_corrompido_e_rejeitado_em_vez_de_virar_saldo(self) -> None:
        """Regressao do bug mais grave desta camada.

        O OCR corrompia "2.100,00 D" em "2.109,008 D"; a regex antiga engolia o
        valor corrompido e casava a coluna de SALDO como valor. Sem sufixo D, um
        debito de R$ 2.100 virava credito de R$ 6.820 — erro que infla a renda.
        """
        texto = EXTRATO_OK.replace("2.100,00 D 6.820,60", "2.109,008 D 6.820,60")
        transacoes, rejeitadas = extrair_transacoes(ocr(texto))

        # A linha corrompida nao pode virar credito.
        assert not any(t.valor.valor > Decimal("5000") for t in transacoes[1:])
        assert len(rejeitadas) == 1

    def test_variacao_de_saldo_resolve_direcao_sem_sufixo(self) -> None:
        # Extrato sem C/D: a coluna de saldo funciona como digito verificador.
        texto = """\
DATA HISTORICO VALOR SALDO
05/01/2025 CREDITO SALARIO 8.000,00 11.000,00
10/01/2025 PAGAMENTO CARTAO 1.500,00 9.500,00
15/01/2025 DEPOSITO 500,00 10.000,00
"""
        transacoes, _ = extrair_transacoes(ocr(texto))

        # A primeira linha nao tem saldo anterior nem sufixo: rejeitada.
        # As seguintes sao resolvidas pela variacao de saldo.
        valores = {t.descricao: t.valor.valor for t in transacoes}
        assert valores["PAGAMENTO CARTAO"] == Decimal("-1500.00")
        assert valores["DEPOSITO"] == Decimal("500.00")

    def test_sem_sufixo_e_sem_saldo_a_linha_e_rejeitada(self) -> None:
        # Assumir credito na duvida infla a renda e aprova credito indevido.
        # Rejeitar faz a incerteza aparecer no parecer.
        texto = "DATA HISTORICO VALOR\n05/01/2025 ALGUM LANCAMENTO 1.000,00\n"
        transacoes, rejeitadas = extrair_transacoes(ocr(texto))

        assert transacoes == []
        assert len(rejeitadas) == 1

    def test_data_invalida_e_rejeitada(self) -> None:
        texto = "DATA HISTORICO VALOR SALDO\n32/13/2025 LANCAMENTO 100,00 C 200,00\n"
        transacoes, rejeitadas = extrair_transacoes(ocr(texto))

        assert transacoes == []
        assert len(rejeitadas) == 1

    def test_ano_de_dois_digitos(self) -> None:
        texto = "DATA HIST VALOR SALDO\n05/01/25 CREDITO SALARIO 8.000,00 C 11.000,00\n"
        transacoes, _ = extrair_transacoes(ocr(texto))
        assert transacoes[0].data == date(2025, 1, 5)

    def test_data_colada_na_descricao(self) -> None:
        """A linha exata que o Tesseract produziu no CI, e que fazia o extrato inteiro sumir.

        O texto vem de uma execucao real dentro do container Debian — nao foi construido a mao:

            05/01/2025CREDITO SALARIO EMPRESA 8.032,14 € 11.232,14

        Duas corrupcoes ao mesmo tempo, e cada uma tem um destino diferente:

        - **data colada na descricao.** O padrao antigo exigia espaco depois do ano, entao nenhuma
          linha casava. O sintoma era o pior possivel: 0 lancamentos e **0 rejeitados**, ou seja
          renda apurada zero sem uma linha para investigar;
        - **`C` lido como `€`.** Sem sufixo legivel e sendo a primeira linha (nao ha saldo anterior
          para comparar), o dominio **rejeita** — assumir credito na duvida inflaria a renda. Aqui
          isso e o comportamento certo, e nao um efeito colateral da correcao.

        A diferenca que este teste prende, entao, e de 0 lancamentos e 0 rejeitados (perda total,
        silenciosa) para 2 lancamentos e 1 rejeitado (o que nao deu para ler, aparecendo como tal).

        O teste esta aqui e nao na avaliacao de OCR porque a avaliacao depende da fonte instalada
        (Arial no Windows, DejaVu no Linux) e por isso le numeros diferentes por maquina. Este texto
        e fixo.
        """
        texto = (
            "DATA HISTORICO VALOR SALDO\n"
            "05/01/2025CREDITO SALARIO EMPRESA 8.032,14 € 11.232,14\n"
            "10/01/2025PAGAMENTO CARTÃO CREDITO 2.311,54 D 8.920,60\n"
            "15/01/2025ALUGUEL IMOVEL RESIDENCIAL 2.100,00 D 6.820,60\n"
        )
        transacoes, rejeitadas = extrair_transacoes(ocr(texto))

        assert [t.valor.valor for t in transacoes] == [
            Decimal("-2311.54"),
            Decimal("-2100.00"),
        ]
        # A linha do salario aparece como rejeitada, e nao desaparece.
        assert len(rejeitadas) == 1
        assert "8.032,14" in rejeitadas[0]

        # A descricao nao pode carregar a data: ela vai para o parecer e para a auditoria.
        assert transacoes[0].descricao.startswith("PAGAMENTO")
        assert transacoes[0].data == date(2025, 1, 10)

    def test_ano_de_dois_digitos_colado_no_valor(self) -> None:
        """Por que o ano nao pode ser `\\d{2,4}` guloso, agora que o espaco e opcional.

        Com `\\d{2,4}` e `\\s*`, em `05/01/2512,50 D` o ano viraria `2512` e o resto `,50 D` — o
        valor do lancamento perdido, em silencio.

        A alternancia `(?:19|20)\\d{2}|\\d{2}` decide pela forma: `25` nao comeca com 19 nem 20,
        entao o ramo de quatro digitos falha e sobra o de dois, com o resto `12,50 D 987,50`.
        """
        texto = "DATA HIST VALOR SALDO\n05/01/2512,50 D 987,50\n"
        transacoes, rejeitadas = extrair_transacoes(ocr(texto))

        assert rejeitadas == []
        assert transacoes[0].data == date(2025, 1, 5)
        assert transacoes[0].valor.valor == Decimal("-12.50")

    def test_data_fora_do_periodo_declarado_e_rejeitada(self) -> None:
        """A linha exata do runner, e o mecanismo que a pega.

            10/05/202PAGAMENTO CARTAO CREDITO 2.137,54 D ...

        O Tesseract comeu o `5` de `2025`. O ramo de quatro digitos falha, sobra o de dois (`20`), e
        a data vira **10/05/2020** — plausivel em forma e impossivel em contexto. Aceita-la inflou
        `meses_analisados` de 6 para 7 no CI, e extrato que parece cobrir mais periodo do que cobre
        passa por politica de minimo de meses que deveria reprovar.

        Recusar pela **forma** nao funciona, e isso foi medido: `(?!\\d)` depois do ano derruba
        `20/01/20255UPERMERCADO`, onde o `5` e o S de SUPERMERCADO e o ano esta correto. As duas
        linhas sao indistinguiveis pela forma.

        O que distingue esta no documento — `Periodo: 01/01/2025 a 20/06/2025` — e e a conferencia
        que um analista faz na mao.
        """
        texto = (
            "Periodo: 01/01/2025 a 20/06/2025\n"
            "DATA HISTORICO VALOR SALDO\n"
            "05/05/2025 CREDITO SALARIO 8.000,00 C 11.000,00\n"
            "10/05/202PAGAMENTO CARTAO CREDITO 2.137,54 D 8.862,46\n"
        )
        transacoes, rejeitadas = extrair_transacoes(ocr(texto))

        assert {t.data.year for t in transacoes} == {2025}
        # A linha nao desaparece: ela aparece como rejeitada, para o parecer poder dizer isso.
        assert len(rejeitadas) == 1
        assert "202PAGAMENTO" in rejeitadas[0]

    def test_sem_cabecalho_de_periodo_nao_ha_filtro(self) -> None:
        """Sem periodo legivel, o parser nao inventa um.

        Inferir o periodo das proprias datas seria circular: as datas sao o que esta sob suspeita.
        Entao a mesma linha do teste acima, sem cabecalho, e aceita — e essa e a escolha certa,
        porque a alternativa e rejeitar lancamento bom por um periodo adivinhado.
        """
        texto = (
            "DATA HISTORICO VALOR SALDO\n"
            "05/05/2025 CREDITO SALARIO 8.000,00 C 11.000,00\n"
            "10/05/202PAGAMENTO CARTAO CREDITO 2.137,54 D 8.862,46\n"
        )
        transacoes, _ = extrair_transacoes(ocr(texto))

        assert {t.data.year for t in transacoes} == {2020, 2025}

    def test_rejeicao_por_periodo_nao_quebra_a_cadeia_de_saldo(self) -> None:
        """A cascata que uma correcao anterior causou, e que a ordem das operacoes evita.

        Quando a linha era recusada antes de chegar ao decompositor, o **saldo dela saia da cadeia**
        — e os creditos de salario seguintes, cujo sufixo `C` o OCR leu como `€`, perdiam a unica
        forma de resolver a direcao. No CI isso levou de 23 transacoes para 11, com zero creditos, e
        o dominio recusou o extrato inteiro por falta de renda comprovada.

        Aqui a linha do meio e rejeitada por periodo e a seguinte, sem sufixo legivel, ainda resolve
        pela variacao de saldo — porque `saldo_anterior` e atualizado antes da rejeicao.
        """
        texto = (
            "Periodo: 01/05/2025 a 31/05/2025\n"
            "DATA HISTORICO VALOR SALDO\n"
            "05/05/2025 CREDITO SALARIO 8.000,00 C 11.000,00\n"
            "10/05/202PAGAMENTO CARTAO 2.000,00 D 9.000,00\n"
            "15/05/2025CREDITO BONUS 1.500,00 € 10.500,00\n"
        )
        transacoes, rejeitadas = extrair_transacoes(ocr(texto))

        valores = {t.descricao: t.valor.valor for t in transacoes}
        assert len(rejeitadas) == 1
        # O bonus resolve pela variacao 9.000,00 -> 10.500,00, que so existe porque o saldo da
        # linha rejeitada continuou na cadeia.
        assert valores["CREDITO BONUS"] == Decimal("1500.00")

    def test_texto_sem_lancamento_devolve_vazio(self) -> None:
        transacoes, rejeitadas = extrair_transacoes(ocr("EXTRATO\nnenhum lancamento aqui"))
        assert transacoes == []
        assert rejeitadas == []


class TestSuficiencia:
    def test_holerite_completo_e_suficiente(self) -> None:
        assert holerite_suficiente(HOLERITE_OK)

    def test_holerite_sem_renda_nao_e_suficiente(self) -> None:
        assert not holerite_suficiente("RECIBO\nNome: JOAO DA SILVA\n")


class TestEscalonamento:
    async def test_para_no_primeiro_motor_que_serve(self) -> None:
        barato = OCRFake(HOLERITE_OK, Percentual.de(90), "barato", custo=1)
        caro = OCRFake(HOLERITE_OK, Percentual.de(99), "caro", custo=100)

        cadeia = MotorOCRComEscalonamento([barato, caro], suficiencia=holerite_suficiente)
        resultado = await cadeia.extrair(IMAGEM_QUALQUER)

        assert resultado.motor == "barato"
        assert caro.chamadas == 0  # o caro nao foi acionado

    async def test_escala_quando_o_barato_nao_serve(self) -> None:
        barato = OCRFake("texto sem campo nenhum", Percentual.de(70), "barato", custo=1)
        caro = OCRFake(HOLERITE_OK, Percentual.de(90), "caro", custo=100)

        cadeia = MotorOCRComEscalonamento([barato, caro], suficiencia=holerite_suficiente)
        resultado = await cadeia.extrair(IMAGEM_QUALQUER)

        assert resultado.motor == "caro"
        assert barato.chamadas == 1
        assert caro.chamadas == 1

    async def test_confianca_alta_nao_basta_se_faltam_campos(self) -> None:
        """O falso positivo medido no eval: 87,8% de confianca e CPF ausente.

        Um limiar de confianca sozinho aceitaria este resultado.
        """
        confiante_mas_incompleto = OCRFake(
            "RECIBO\nalgum texto legivel", Percentual.de(98), "confiante", custo=1
        )
        caro = OCRFake(HOLERITE_OK, Percentual.de(90), "caro", custo=100)

        cadeia = MotorOCRComEscalonamento(
            [confiante_mas_incompleto, caro], suficiencia=holerite_suficiente
        )
        assert (await cadeia.extrair(IMAGEM_QUALQUER)).motor == "caro"

    async def test_confianca_baixa_nao_escala_se_os_campos_sairam(self) -> None:
        """O falso negativo medido no eval: extrato perfeito a 83,9%.

        Tabela densa recebe score por palavra mais baixo que prosa; um limiar
        global mandaria uma extracao perfeita para revisao humana.
        """
        completo_mas_pouco_confiante = OCRFake(HOLERITE_OK, Percentual.de(70), "tesseract", custo=1)
        caro = OCRFake(HOLERITE_OK, Percentual.de(99), "caro", custo=100)

        cadeia = MotorOCRComEscalonamento(
            [completo_mas_pouco_confiante, caro], suficiencia=holerite_suficiente
        )
        resultado = await cadeia.extrair(IMAGEM_QUALQUER)

        assert resultado.motor == "tesseract"
        assert caro.chamadas == 0

    async def test_texto_rejeitado_pela_politica_sempre_escala(self) -> None:
        # Abaixo de 60% a POL-002 manda reenviar; nem o verificador salva.
        ruim = OCRFake(HOLERITE_OK, Percentual.de(40), "ruim", custo=1)
        caro = OCRFake(HOLERITE_OK, Percentual.de(95), "caro", custo=100)

        cadeia = MotorOCRComEscalonamento([ruim, caro], suficiencia=holerite_suficiente)
        assert (await cadeia.extrair(IMAGEM_QUALQUER)).motor == "caro"

    async def test_devolve_o_melhor_quando_nenhum_serve(self) -> None:
        a = OCRFake("nada util", Percentual.de(65), "a", custo=1)
        b = OCRFake("nada util tambem", Percentual.de(80), "b", custo=100)

        cadeia = MotorOCRComEscalonamento([a, b], suficiencia=holerite_suficiente)
        resultado = await cadeia.extrair(IMAGEM_QUALQUER)

        # O de maior confianca, nao o ultimo da fila.
        assert resultado.motor == "b"

    async def test_falha_de_um_motor_nao_encerra_a_cadeia(self) -> None:
        """Motor indisponivel e justamente o caso em que escalar faz sentido."""

        class Quebrado:
            identificacao = "quebrado"
            custo_relativo = 1

            async def extrair(self, imagem: object) -> ResultadoOCR:
                raise RuntimeError("tesseract nao instalado")

        caro = OCRFake(HOLERITE_OK, Percentual.de(95), "caro", custo=100)
        cadeia = MotorOCRComEscalonamento([Quebrado(), caro], suficiencia=holerite_suficiente)  # type: ignore[list-item]

        assert (await cadeia.extrair(IMAGEM_QUALQUER)).motor == "caro"

    async def test_falha_do_unico_motor_propaga(self) -> None:
        class Quebrado:
            identificacao = "quebrado"
            custo_relativo = 1

            async def extrair(self, imagem: object) -> ResultadoOCR:
                raise RuntimeError("sem motor disponivel")

        cadeia = MotorOCRComEscalonamento([Quebrado()])  # type: ignore[list-item]
        with pytest.raises(RuntimeError, match="sem motor"):
            await cadeia.extrair(IMAGEM_QUALQUER)

    async def test_registra_as_tentativas_para_auditoria(self) -> None:
        barato = OCRFake("insuficiente", Percentual.de(70), "barato", custo=1)
        caro = OCRFake(HOLERITE_OK, Percentual.de(95), "caro", custo=100)

        cadeia = MotorOCRComEscalonamento([barato, caro], suficiencia=holerite_suficiente)
        await cadeia.extrair(IMAGEM_QUALQUER)

        assert [t.motor for t in cadeia.tentativas] == ["barato", "caro"]
        assert cadeia.tentativas[0].escalou
        assert not cadeia.tentativas[1].escalou

    async def test_cadeia_vazia_e_erro_de_programacao(self) -> None:
        with pytest.raises(ValueError, match="ao menos um motor"):
            MotorOCRComEscalonamento([])

    async def test_sem_verificador_usa_a_confianca(self) -> None:
        baixa = OCRFake("algum texto", Percentual.de(70), "baixa", custo=1)
        alta = OCRFake("algum texto", Percentual.de(95), "alta", custo=100)

        cadeia = MotorOCRComEscalonamento([baixa, alta])  # sem suficiencia
        assert (await cadeia.extrair(IMAGEM_QUALQUER)).motor == "alta"
