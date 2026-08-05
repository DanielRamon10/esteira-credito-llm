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
from datetime import date, timedelta
from decimal import Decimal

import structlog

from credit_analysis.domain.documento import (
    CampoExtraido,
    ExtracaoHolerite,
    OrigemDaRenda,
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
#
# ## Espaco em volta dos separadores, e a medicao que o exigiu
#
# A versao anterior era `\d{1,3}(?:\.\d{3})*,\d{2}` — sem tolerancia a espaco — e ela perdia o
# salario liquido no perfil `pouca_luz` da avaliacao de OCR. O texto que o Tesseract produz e:
#
#     VALOR LIQUIDO A RECEBER R$ 7.262 , 14
#
# O rotulo sai **perfeito**; o que ele quebra e o numero, inserindo espaco em volta da virgula.
# Binarizacao adaptativa em imagem escura engrossa os tracos e a virgula vira um blob que o
# segmentador separa dos digitos vizinhos.
#
# A consequencia media era grave: o campo nao casava, `renda_comprovada` caia para o salario base, e
# a renda apurada ficava 17% acima da real (R$ 8.500,00 contra R$ 7.262,14) na direcao que aprova
# credito que nao deveria.
#
# **Ler o numero certo e melhor que sinalizar o numero errado.** A rede de seguranca (`renda_origem`
# + revisao humana) continua existindo para o caso em que so o bruto e legivel de verdade; este
# padrao remove o caso em que ela seria acionada por um espaco.
#
# ## `[^\S\n]` e nao `\s`
#
# Espaco horizontal apenas. Com `\s`, o padrao atravessaria fim de linha e casaria o milhar de uma
# linha com os centavos da seguinte — inventando um valor a partir de duas linhas diferentes, que e
# a familia de defeito que o cabecalho de `_INICIO_LANCAMENTO` documenta em detalhe.
_ESP = r"[^\S\n]*"
_VALOR = rf"\d{{1,3}}(?:{_ESP}\.{_ESP}\d{{3}})*{_ESP},{_ESP}\d{{2}}"

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
#
# ## O espaco depois da data e opcional, e isso foi encontrado por medicao
#
# A versao anterior exigia `\s+` entre o ano e o resto da linha, e ela funcionava — na maquina de
# quem a escreveu. No CI, a mesma avaliacao leu 0 lancamentos de 24, com `linhas_rejeitadas=0`:
# nenhuma linha casava o padrao, e o extrato inteiro sumia sem nada ser rejeitado.
#
# A causa esta no texto que o Tesseract produziu:
#
#     05/01/2025CREDITO SALARIO EMPRESA 8.032,14 € 11.232,14
#
# A data cola na descricao. O gerador de documento sintetico resolve a fonte pelo que existe no
# sistema (Arial no Windows, DejaVu no Linux), as metricas das duas diferem, e com DejaVu as
# colunas ficam proximas o suficiente para o Tesseract nao emitir o espaco.
#
# **Nao e artefato de teste.** Extrato escaneado de verdade cola coluna assim — e um parser que
# exige espaco depois da data descarta a linha inteira em silencio, que e o pior desfecho possivel:
# renda apurada zero em vez de renda apurada errada, e nenhuma linha rejeitada para investigar.
#
# ## Por que o ano nao e `\d{2,4}` com `\s*`
#
# Trocar so o `\s+` por `\s*` cria um defeito novo. Com `\d{2,4}` guloso e ano de dois digitos
# colado na coluna seguinte, `05/01/2512,50 D` faria o ano virar `2512` e o resto `,50 D` — o valor
# do lancamento seria perdido.
#
# A alternancia resolve pela forma do ano e nao pelo espaco: quatro digitos **somente** se
# comecarem com 19 ou 20, senao dois. No exemplo acima o ramo de quatro falha (`25` nao e 19 nem
# 20), cai para dois digitos, e o resto fica `12,50 D`.
#
# ## O que **nao** funciona: exigir que o ano nao seja seguido de digito
#
# Com a alternancia sozinha, o CI passou de 0 para 23 lancamentos e falhou noutro ponto:
# `meses_analisados == 7` num extrato de 6 meses, por causa desta linha do mesmo runner:
#
#     10/05/202PAGAMENTO CARTAO CREDITO 2.137,54 D ...
#
# Tres digitos no campo do ano: o Tesseract comeu o `5` de `2025`. O ramo de quatro digitos falha,
# sobra `\d{2}` = `20`, o resto fica `2PAGAMENTO...` e a data vira **10/05/2020**.
#
# A correcao obvia seria `(?!\d)` depois do ano, recusando ano seguido de digito. Foi medida no
# mesmo ambiente e **derrubou 12 lancamentos legitimos**, porque estas linhas tem forma identica:
#
#     20/01/20255UPERMERCADO COMPRA 612,51 D ...
#
# Aqui o ano e 2025 e o `5` e o **S de SUPERMERCADO** — digito depois do ano, e o ano esta correto.
# Nada na forma da linha distingue "digito sobrando no ano" de "descricao comecando com digito", e a
# mesma regra que salva um caso destroi o outro.
#
# E o efeito foi pior que perder as 12: linha recusada no estagio da regex nunca chega ao
# `_decompor_linhas`, entao o **saldo dela sai da cadeia** — e sem cadeia de saldo os 6 creditos de
# salario (cujo sufixo `C` o OCR leu como `€`) perderam a unica forma de resolver a direcao. De 23
# transacoes para 11, com zero creditos, e o dominio recusando o extrato inteiro por falta de renda.
#
# A licao: a forma da linha nao carrega a informacao necessaria. O **documento** carrega, e e de la
# que a validacao vem — ver `_PERIODO` logo abaixo.
_INICIO_LANCAMENTO = re.compile(
    r"^[^\S\n]*(?P<dia>\d{2})\s*/\s*(?P<mes>\d{2})\s*/\s*"
    r"(?P<ano>(?:19|20)\d{2}|\d{2})\s*(?P<resto>.+)$",
    re.MULTILINE,
)

# O periodo declarado no cabecalho do extrato.
#
# `Periodo: 01/01/2025 a 20/06/2025` — presente em extrato de qualquer banco, porque e o que define
# o que o documento cobre. E a fonte que distingue as duas linhas de forma identica acima: uma data
# de 2020 contradiz o proprio documento e uma de 2025 nao, e essa e a conferencia que um analista
# faz na mao.
#
# Tolerante na acentuacao (`per[ií]?odo`) porque o OCR erra o `í`, e no separador porque ele varia
# por banco. Quando o cabecalho nao existe ou nao e legivel, nao ha filtro — a alternativa seria
# inventar um periodo, que e o erro que este bloco todo existe para nao cometer.
_PERIODO = re.compile(
    r"per[ií]?odo[:\s]+(\d{2})\s*/\s*(\d{2})\s*/\s*(\d{4})\s*(?:a|-|ate|até)\s*"
    r"(\d{2})\s*/\s*(\d{2})\s*/\s*(\d{4})",
    re.IGNORECASE,
)

# ## Uma tentativa que foi removida, e por que ela nao servia
#
# Houve aqui um `_LINHA_COM_DATA_ILEGIVEL = r"^\s*\d{2}/\d{2}/\d{2,}"`, para rejeitar linha com cara
# de lancamento que nao casasse o padrao — a ideia era o defeito da data colada nao poder voltar em
# silencio.
#
# Com a alternancia no ano ela ficou **inalcancavel**: se dois digitos aparecem depois da segunda
# barra, o ramo `\d{2}` casa sempre. O unico caso que sobrava era linha que termina na data, e essa
# nao tem valor monetario — ou seja, nao e lancamento perdido.
#
# Ficou registrado em vez de apagado porque a propriedade que ela buscava e legitima e passou a ser
# garantida em outro lugar: data que contradiz o periodo do documento entra em `rejeitadas`, e nao
# no vazio. Guarda de seguranca inalcancavel com um comentario confiante e pior que nenhuma.

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
    periodo = _periodo_declarado(ocr.texto)

    for decomposta in _decompor_linhas(ocr.texto, rejeitadas):
        direcao = _resolver_direcao(decomposta, saldo_anterior)

        # O saldo entra na cadeia **antes** de qualquer rejeicao, e essa ordem e o que impede o
        # efeito em cascata: uma linha descartada ainda serve de digito verificador para a
        # seguinte. Medido pelo avesso — quando uma correcao fez linhas serem recusadas antes deste
        # ponto, os 6 creditos de salario seguintes perderam a referencia e o extrato inteiro caiu.
        if decomposta.saldo is not None:
            saldo_anterior = decomposta.saldo

        # Data que contradiz o periodo do proprio documento nao e lancamento: e leitura errada.
        #
        # Um digito a mais no ano (`2025` lido como `20202`) produz uma data de 2020 plausivel em
        # forma e impossivel em contexto. Aceita-la infla `meses_analisados`, e extrato que parece
        # cobrir mais periodo do que cobre passa por politica de minimo de meses que deveria
        # reprovar.
        if periodo is not None and not (periodo[0] <= decomposta.data <= periodo[1]):
            rejeitadas.append(decomposta.linha)
            continue

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


def _periodo_declarado(texto: str) -> tuple[date, date] | None:
    """O intervalo que o cabecalho do extrato declara, ou None quando nao da para ler.

    Uma folga de um dia em cada ponta: extrato costuma listar lancamento com data de efetivacao um
    dia fora do periodo pedido, e rejeitar por causa disso descartaria linha boa. A folga e pequena
    de proposito — ela cobre arredondamento de fronteira, nao erro de ano.

    Devolver None quando o cabecalho falta e deliberado: sem periodo, nao ha filtro. Inferir o
    periodo das proprias datas seria circular — as datas sao justamente o que esta sob suspeita.
    """
    achado = _PERIODO.search(texto)
    if achado is None:
        return None

    inicio = _parsear_data(achado.group(1), achado.group(2), achado.group(3))
    fim = _parsear_data(achado.group(4), achado.group(5), achado.group(6))
    if inicio is None or fim is None or fim < inicio:
        return None

    return inicio - timedelta(days=1), fim + timedelta(days=1)


def _decompor_linhas(texto: str, rejeitadas: list[str]) -> list[_LinhaExtrato]:
    """Separa cada linha de lancamento em data, historico, valor e saldo.

    A ultima coluna monetaria e o saldo quando ha duas ou mais; a penultima e o
    valor do lancamento. Essa leitura por posicao e o que impede a confusao
    entre as duas colunas.
    """
    resultado: list[_LinhaExtrato] = []

    # Varredura por linha e nao `finditer` no texto inteiro. O resultado e o mesmo — o padrao e
    # ancorado em `^...$` com MULTILINE, portanto nunca cruza linha — e por linha a leitura fica
    # obvia: uma linha, uma tentativa de casar.
    for bruta in texto.splitlines():
        match = _INICIO_LANCAMENTO.match(bruta)
        if match is None:
            continue

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
    """Se o texto tem o minimo para compor um parecer: renda **liquida** e identificacao.

    Usado como `Suficiencia` na cadeia de OCR. E isto — e nao a media de
    confianca da pagina — que decide escalar para o modelo de visao.

    ## Por que aqui a exigencia e mais alta que em `ExtracaoHolerite.completa`

    `completa` responde "da para emitir parecer com isto?", e a resposta e sim mesmo com o bruto:
    recusar um documento legivel por causa de um rotulo custaria disponibilidade. Esta funcao
    responde outra pergunta — "vale a pena tentar um motor melhor?" — e ai o bruto **nao** serve,
    porque existe um numero melhor na imagem que este motor nao conseguiu ler.

    A diferenca foi medida: sob `pouca_luz` o liquido nao casava e a renda vinha do bruto. Com a
    exigencia de `completa` aqui, a cadeia considerava o resultado suficiente e **nao escalava**,
    apesar de haver um motor de visao capaz de ler o campo que faltava. Aquele caso especifico ja
    nao ocorre (ver `_VALOR`), e a regra vale para os que restam.

    Escalar nao tem downside quando nao ha para onde escalar: `MotorOCRComEscalonamento` devolve o
    resultado de maior confianca quando nenhum motor satisfaz. Sem modelo de visao configurado — o
    default deste projeto — o comportamento e identico ao de antes, e o caso vai para revisao humana
    por `ResultadoProcessamento.exige_revisao_humana`.
    """
    ocr = ResultadoOCR(texto=texto, confianca=Percentual.de(100), motor="verificacao")
    extracao = extrair_holerite(ocr)
    return extracao.completa and extracao.origem_da_renda is OrigemDaRenda.LIQUIDO


def extrato_suficiente(minimo_transacoes: int = 3) -> object:
    """Fabrica um verificador que exige um numero minimo de lancamentos legiveis."""

    def verificar(texto: str) -> bool:
        ocr = ResultadoOCR(texto=texto, confianca=Percentual.de(100), motor="verificacao")
        transacoes, _ = extrair_transacoes(ocr)
        return len(transacoes) >= minimo_transacoes

    return verificar
