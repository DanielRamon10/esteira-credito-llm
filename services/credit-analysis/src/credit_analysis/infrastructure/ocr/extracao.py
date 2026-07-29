"""Extracao de campos estruturados do texto de OCR.

Por que regex e nao LLM. O texto de um holerite ou extrato tem estrutura
previsivel — rotulo seguido de valor, tabela com colunas fixas. Regex resolve
isso de forma deterministica, auditavel e sem custo, e um teste cobre cada
padrao. Passar o texto para um LLM extrair introduz nao-determinismo e custo
num problema que nao precisa de nenhum dos dois.

Onde o LLM entra e no caso oposto: documento com layout desconhecido, ou quando
os padroes falham. Ai vale a flexibilidade. Este modulo cobre o caminho comum;
o escalonamento cobre a excecao.

Toda extracao carrega o trecho de origem, para que o analista confira sem
reabrir o documento.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import structlog

from credit_analysis.domain.documento import (
    CampoExtraido,
    ExtracaoHolerite,
    ResultadoOCR,
    parsear_valor_brl,
)
from credit_analysis.domain.exceptions import ValorInvalido
from credit_analysis.domain.extrato import Transacao
from credit_analysis.domain.value_objects import CPF, Dinheiro, Percentual

logger = structlog.get_logger(__name__)

# Confianca atribuida a um campo casado por padrao estrutural. Menor que a
# confianca do OCR de proposito: casar o padrao prova que o rotulo foi lido, nao
# que o valor esta correto.
CONFIANCA_PADRAO_ESTRUTURAL = Percentual.de(90)

# Um valor monetario em pt-BR: milhares opcionais, decimais obrigatorios.
_VALOR = r"\d{1,3}(?:\.\d{3})*,\d{2}"

# O OCR troca digito por letra parecida com frequencia. Corrigimos apenas onde o
# contexto garante que e digito (dentro de CPF/CNPJ/valor), nunca em texto livre.
_CONFUSOES_DIGITO = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1", "S": "5", "B": "8"})


def _normalizar(texto: str) -> str:
    """Sem acento e maiusculo, para casar rotulo independente da acentuacao do OCR."""
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return sem_acento.upper()


def _linha_de(texto: str, posicao: int) -> str:
    """Linha completa em que a posicao cai — vira o `trecho_origem` do campo."""
    inicio = texto.rfind("\n", 0, posicao) + 1
    fim = texto.find("\n", posicao)
    return texto[inicio : fim if fim != -1 else len(texto)].strip()


def _buscar(texto: str, padrao: str, nome: str, *, grupo: int = 1) -> CampoExtraido | None:
    """Aplica um padrao sobre o texto normalizado e devolve o campo com origem."""
    normalizado = _normalizar(texto)
    match = re.search(padrao, normalizado, re.MULTILINE)
    if match is None:
        return None

    return CampoExtraido(
        nome=nome,
        valor_bruto=match.group(grupo).strip(),
        # A linha vem do texto ORIGINAL, na mesma posicao: a normalizacao
        # preserva o comprimento (NFKD + ascii remove acento sem mudar offset
        # para os caracteres que nos interessam), entao o analista ve o texto
        # como esta no documento, com acento.
        trecho_origem=_linha_de(texto, match.start()),
        confianca=CONFIANCA_PADRAO_ESTRUTURAL,
    )


# --- Holerite ----------------------------------------------------------------

_PADROES_HOLERITE: dict[str, str] = {
    "nome": r"NOME[:\s]+([A-Z][A-Z\s]{4,60}?)(?:\s{2,}|COMPETENCIA|CPF|$)",
    "cpf": r"CPF[:\s]+([\d\.\-OoIlSB]{11,18})",
    "competencia": r"COMPETENCIA[:\s]+(\d{2}\s*/\s*\d{4})",
    "salario_base": rf"SAL[AÁ]RIO\s+BASE\s+(?:R\$\s*)?({_VALOR})",
    "salario_liquido": rf"L[IÍ]QUIDO(?:\s+A\s+RECEBER)?\s*:?\s*(?:R\$|RS)?\s*({_VALOR})",
}

# Rotulos usados para achar o empregador: costuma ser a primeira linha
# significativa, ou a linha anterior ao CNPJ.
_PADRAO_CNPJ = r"CNPJ[:\s]+([\d\.\/\-]{14,20})"


def extrair_holerite(ocr: ResultadoOCR) -> ExtracaoHolerite:
    """Localiza os campos de interesse num holerite."""
    texto = ocr.texto
    campos: dict[str, CampoExtraido | None] = {}
    nao_reconhecidos: list[str] = []

    for nome, padrao in _PADROES_HOLERITE.items():
        campo = _buscar(texto, padrao, nome)
        campos[nome] = campo
        if campo is None:
            nao_reconhecidos.append(nome)

    if (cpf := campos.get("cpf")) is not None:
        campos["cpf"] = _validar_cpf(cpf, nao_reconhecidos)

    campos["empregador"] = _extrair_empregador(texto)
    if campos["empregador"] is None:
        nao_reconhecidos.append("empregador")

    extracao = ExtracaoHolerite(
        nome=campos.get("nome"),
        cpf=campos.get("cpf"),
        empregador=campos.get("empregador"),
        competencia=campos.get("competencia"),
        salario_base=campos.get("salario_base"),
        salario_liquido=campos.get("salario_liquido"),
        campos_nao_reconhecidos=tuple(nao_reconhecidos),
    )

    logger.info(
        "extracao.holerite",
        motor=ocr.motor,
        reconhecidos=len(_PADROES_HOLERITE) + 1 - len(nao_reconhecidos),
        nao_reconhecidos=nao_reconhecidos,
        renda=str(extracao.renda_comprovada) if extracao.renda_comprovada else None,
    )
    return extracao


def _validar_cpf(campo: CampoExtraido, nao_reconhecidos: list[str]) -> CampoExtraido | None:
    """Confere os digitos verificadores, tentando corrigir confusao de OCR.

    O DV do CPF e uma soma de verificacao: ele detecta quase todo erro de um
    digito. Isso torna o CPF o campo mais confiavel do documento — se valida, a
    leitura esta certa; se nao valida, sabemos que errou, o que nao acontece com
    um campo de renda.
    """
    bruto = campo.valor_bruto
    for candidato in (bruto, bruto.translate(_CONFUSOES_DIGITO)):
        try:
            validado = CPF(candidato)
        except ValorInvalido:
            continue

        corrigido = candidato != bruto
        return CampoExtraido(
            nome=campo.nome,
            valor_bruto=validado.numero,
            trecho_origem=campo.trecho_origem,
            # Correcao de confusao de digito reduz a confianca: acertamos o DV,
            # mas houve suposicao no meio.
            confianca=Percentual.de(75) if corrigido else Percentual.de(99),
        )

    logger.info("extracao.cpf_invalido", bruto=bruto[:6] + "...")
    nao_reconhecidos.append("cpf")
    return None


def _extrair_empregador(texto: str) -> CampoExtraido | None:
    """Empregador: a linha imediatamente anterior ao CNPJ, ou a segunda linha util."""
    linhas = [x.strip() for x in texto.splitlines() if x.strip()]
    normalizadas = [_normalizar(x) for x in linhas]

    for indice, linha in enumerate(normalizadas):
        if re.search(_PADRAO_CNPJ, linha) and indice > 0:
            return CampoExtraido(
                nome="empregador",
                valor_bruto=linhas[indice - 1],
                trecho_origem=linhas[indice - 1],
                confianca=Percentual.de(80),
            )

    # Sem CNPJ: a razao social costuma ser a linha seguinte ao titulo.
    if len(linhas) >= 2 and len(linhas[1]) >= 8:
        return CampoExtraido(
            nome="empregador",
            valor_bruto=linhas[1],
            trecho_origem=linhas[1],
            confianca=Percentual.de(50),  # heuristica fraca, confianca baixa
        )

    return None


# --- Extrato bancario --------------------------------------------------------

# Inicio de uma linha de lancamento: apenas a data. O resto e analisado por
# posicao, nao por um padrao unico que tenta casar linha inteira.
#
# A versao anterior tentava `historico.+?  valor  [CD]?` numa unica regex e
# tinha um bug grave: quando o OCR corrompia a coluna de valor
# ("2.100,00 D" lido como "2.1009,"), o `.+?` engolia o valor corrompido e a
# regex casava a coluna de SALDO como se fosse o valor. Sem sufixo D, um
# debito de R$ 2.100 virava um credito de R$ 6.820 — erro que **infla a renda
# aparente**, exatamente a direcao que uma esteira de credito nao pode errar.
_INICIO_LANCAMENTO = re.compile(
    r"^\s*(?P<dia>\d{2})\s*/\s*(?P<mes>\d{2})\s*/\s*(?P<ano>\d{2,4})\s+(?P<resto>.+)$",
    re.MULTILINE,
)

# Valor monetario com sufixo C/D opcional, em qualquer posicao da linha.
_VALOR_COM_TIPO = re.compile(rf"(?P<valor>-?{_VALOR})\s*(?P<tipo>[CD])?(?![\d,\.])")

# Palavras que indicam linha de resumo, nao lancamento. Somar "SALDO ANTERIOR"
# como credito inflaria a renda apurada.
_TERMOS_RESUMO = ("SALDO ANTERIOR", "SALDO FINAL", "SALDO ATUAL", "TOTAL", "SUBTOTAL")

# Tolerancia ao conferir valor contra a variacao de saldo. Um centavo cobre
# arredondamento; mais que isso e leitura errada, nao arredondamento.
TOLERANCIA_CONFERENCIA = Decimal("0.02")


@dataclass(frozen=True, slots=True)
class _LinhaExtrato:
    """Linha de lancamento decomposta por posicao de coluna."""

    linha: str
    data: date
    historico: str
    valor: Decimal
    tipo: str | None
    saldo: Decimal | None


def extrair_transacoes(ocr: ResultadoOCR) -> tuple[list[Transacao], list[str]]:
    """Converte o texto de um extrato em transacoes, alimentando a analise Pandas.

    A direcao do lancamento (credito ou debito) e resolvida em tres niveis, do
    mais confiavel ao menos:

    1. **Variacao de saldo** — a coluna de saldo funciona como digito
       verificador da coluna de valor: se `saldo - saldo_anterior` tem o mesmo
       modulo do valor lido, a leitura esta confirmada e o sinal da diferenca da
       a direcao sem depender de sufixo nenhum.
    2. **Sufixo C/D ou sinal negativo** — quando nao ha cadeia de saldo.
    3. **Rejeicao** — quando nem um nem outro resolve.

    O nivel 3 e deliberado: assumir credito na duvida infla a renda apurada, o
    que aprova credito que nao deveria. Rejeitar a linha e reportar a rejeicao
    faz a incerteza aparecer no parecer em vez de virar numero errado.
    """
    transacoes: list[Transacao] = []
    rejeitadas: list[str] = []
    saldo_anterior: Decimal | None = None

    for decomposta in _decompor_linhas(ocr.texto, rejeitadas):
        direcao = _resolver_direcao(decomposta, saldo_anterior)

        if decomposta.saldo is not None:
            saldo_anterior = decomposta.saldo

        if direcao is None:
            rejeitadas.append(decomposta.linha)
            continue

        transacoes.append(
            Transacao(
                data=decomposta.data,
                descricao=decomposta.historico,
                valor=Dinheiro(direcao),
            )
        )

    logger.info(
        "extracao.extrato",
        motor=ocr.motor,
        transacoes=len(transacoes),
        linhas_rejeitadas=len(rejeitadas),
    )
    return transacoes, rejeitadas


def _decompor_linhas(texto: str, rejeitadas: list[str]) -> list[_LinhaExtrato]:
    """Separa cada linha de lancamento em data, historico, valor e saldo.

    A ultima coluna monetaria e o saldo quando ha duas ou mais; a penultima e o
    valor do lancamento. Essa leitura por posicao e o que impede a confusao
    entre as duas colunas.
    """
    resultado: list[_LinhaExtrato] = []

    for match in _INICIO_LANCAMENTO.finditer(texto):
        linha = match.group(0).strip()
        resto = match.group("resto")

        data = _parsear_data(match.group("dia"), match.group("mes"), match.group("ano"))
        if data is None:
            rejeitadas.append(linha)
            continue

        valores = list(_VALOR_COM_TIPO.finditer(resto))
        if not valores:
            # Linha com data mas sem valor: cabecalho de periodo, por exemplo.
            continue

        if len(valores) >= 2:
            coluna_valor, coluna_saldo = valores[-2], valores[-1]
            saldo = parsear_valor_brl(coluna_saldo.group("valor"))
        else:
            coluna_valor, saldo = valores[0], None

        historico = resto[: coluna_valor.start()].strip()
        if any(termo in _normalizar(historico) for termo in _TERMOS_RESUMO):
            continue

        valor = parsear_valor_brl(coluna_valor.group("valor"))
        if valor is None or valor == 0:
            rejeitadas.append(linha)
            continue

        resultado.append(
            _LinhaExtrato(
                linha=linha,
                data=data,
                historico=historico or "(sem historico)",
                valor=valor,
                tipo=coluna_valor.group("tipo"),
                saldo=saldo,
            )
        )

    return resultado


def _resolver_direcao(linha: _LinhaExtrato, saldo_anterior: Decimal | None) -> Decimal | None:
    """Devolve o valor com sinal, ou None quando a direcao nao pode ser afirmada."""
    modulo = abs(linha.valor)

    # Nivel 1: a cadeia de saldo confirma valor e direcao ao mesmo tempo.
    if linha.saldo is not None and saldo_anterior is not None:
        variacao = linha.saldo - saldo_anterior
        if abs(abs(variacao) - modulo) <= TOLERANCIA_CONFERENCIA:
            return variacao

    # Nivel 2: sufixo explicito, ou sinal negativo preservado pelo parser.
    if linha.tipo == "D":
        return -modulo
    if linha.tipo == "C":
        return modulo
    if linha.valor < 0:
        return linha.valor

    # Nivel 3: sem confirmacao. Nao adivinhamos a direcao do dinheiro.
    return None


def _parsear_data(dia: str, mes: str, ano: str) -> date | None:
    try:
        d, m = int(dia), int(mes)
        a = int(ano)
    except ValueError:
        return None

    # Ano de dois digitos: extrato bancario nao e documento historico, entao a
    # janela e sempre 20xx.
    if a < 100:
        a += 2000

    try:
        return date(a, m, d)
    except ValueError:
        return None


# --- Verificadores de suficiencia para o escalonamento -----------------------


def holerite_suficiente(texto: str) -> bool:
    """Se o texto tem o minimo para compor um parecer: renda e identificacao.

    Usado como `Suficiencia` na cadeia de OCR. E isto — e nao a media de
    confianca da pagina — que decide escalar para o modelo de visao.
    """
    ocr = ResultadoOCR(texto=texto, confianca=Percentual.de(100), motor="verificacao")
    return extrair_holerite(ocr).completa


def extrato_suficiente(minimo_transacoes: int = 3) -> object:
    """Fabrica um verificador que exige um numero minimo de lancamentos legiveis."""

    def verificar(texto: str) -> bool:
        ocr = ResultadoOCR(texto=texto, confianca=Percentual.de(100), motor="verificacao")
        transacoes, _ = extrair_transacoes(ocr)
        return len(transacoes) >= minimo_transacoes

    return verificar
