"""Casamento de nome contra lista restritiva — o nucleo deste servico.

## Por que isto e um problema de verdade

Triagem de KYC nao e busca exata. A pessoa se cadastra como "Jose da Silva
Junior", a lista de sancoes tem "JOSE DA SILVA JR.", e o cartorio registrou
"José Da Silva Júnior". Comparar string com `==` erra os tres casos; comparar com
similaridade solta acusa "Maria Silva" contra "Mario Silva" e transforma a fila de
revisao em lixo.

Os dois erros custam coisas diferentes, e essa assimetria decide o desenho:

- **Falso negativo** (nao acusar quem esta na lista) e violacao regulatoria. A
  Circular BCB 3.978 exige monitoramento; deixar passar um sancionado e o tipo de
  falha que gera multa e termo de compromisso.
- **Falso positivo** custa tempo de analista. Caro, mas recuperavel.

Logo o limiar pende para sensibilidade. Mas "pender" nao e "ignorar precisao": um
sistema que acusa 40% da base treina o analista a aprovar tudo sem ler, e ai o
falso negativo volta pela porta dos fundos. O limiar esta medido em
`tests/eval/test_matching_qualidade.py`.

## Por que nao usar embedding

Seria a resposta reflexa, e e errada aqui. Modelo de embedding aproxima
*significado*, e nome proprio nao tem significado a aproximar — "Silva" e
"Souza" ficam vizinhos no espaco vetorial por serem ambos sobrenomes brasileiros
comuns, o que e exatamente o oposto do que a triagem precisa. Casamento de nome e
problema **lexical**: ordem de token, abreviacao, acento, erro de digitacao.

Alem disso, decisao de KYC precisa ser explicavel a um regulador. Este modulo
devolve *quais tokens casaram e como*; um score de cosseno nao explica nada.

## O algoritmo, em tres sinais

1. **Cobertura de token** — quantos tokens do nome consultado aparecem no nome da
   lista, aceitando abreviacao ("JR" casa com "JUNIOR") e erro de um caractere.
2. **Similaridade de caractere** sobre o nome inteiro normalizado, que captura
   transposicao e digito trocado.
3. **Penalidade por token faltante forte** — quando um sobrenome distintivo do
   nome consultado nao aparece de forma alguma na lista, o score cai. E o que
   separa "Jose da Silva" de "Jose da Silva Rodrigues".

A combinacao e ponderada e documentada, nao calibrada por tentativa e erro: cada
peso responde a um caso concreto de erro, listado no comentario dele.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import StrEnum

# Particulas que nao identificam ninguem. "DA", "DE", "DOS" aparecem em metade
# dos nomes brasileiros; conta-las como token casado inflaria o score de qualquer
# par de nomes longos.
#
# **"E" NAO esta nesta lista, e a ausencia e deliberada.** A primeira versao a
# incluia como conjuncao, e o efeito medido foi anular a regra de inicial
# abreviada: "CARLOS E. LIMA" perdia o "E" na tokenizacao, o "EDUARDO" do nome
# consultado ficava sem par, e um casamento legitimo caia de ~0,93 para 0,690 —
# abaixo de um par que NAO deveria casar.
#
# O conflito e real: "E" pode ser conjuncao ou inicial de nome do meio. Em nome
# brasileiro a conjuncao e rara e a inicial e comum em lista oficial, entao tratar
# como inicial erra menos. "Y" e "D" seguem na lista porque ali a conjuncao domina.
PARTICULAS = frozenset({"DA", "DE", "DO", "DAS", "DOS", "D", "Y", "DEL", "LA"})

# Abreviacoes de sufixo geracional e titulo. Sem esta tabela, "JR" contra
# "JUNIOR" seria token faltante e derrubaria um casamento legitimo.
EQUIVALENCIAS = {
    "JR": "JUNIOR",
    "JR.": "JUNIOR",
    "JUNIOR": "JUNIOR",
    "FILHO": "FILHO",
    "NETO": "NETO",
    "SOBRINHO": "SOBRINHO",
    "SR": "SENIOR",
    "SENIOR": "SENIOR",
    "FO": "FILHO",
}

# Peso de cada sinal na combinacao final.
#
# Cobertura pesa mais que similaridade de caractere porque nome e composto: acertar
# "JOSE" e "SILVA" em "JOSE ANTONIO SILVA" diz mais que a distancia de edicao entre
# as duas strings inteiras, que e penalizada pelo token extra.
PESO_COBERTURA = 0.65
PESO_CARACTERE = 0.35

# Quanto se desconta por sobrenome distintivo ausente. Calibrado contra o caso
# "JOSE DA SILVA" vs "JOSE DA SILVA RODRIGUES": sem penalidade os dois davam 1,0,
# porque toda palavra do consultado esta na lista. Com 0,18 o par cai para 0,82 —
# ainda revisavel, mas nao "identico".
PENALIDADE_TOKEN_AUSENTE = 0.18

# Um token de tres letras ou menos nao sustenta comparacao aproximada nenhuma:
# "ANA" e "ANO" ficariam a um caractere de distancia. Abaixo disso, igualdade.
MIN_TOKEN_PARA_FUZZY = 4

# Tamanho minimo para aceitar insercao, remocao ou substituicao interna. Em token
# de 4 ou 5 letras uma edicao muda 20% a 25% da palavra, o que confunde nomes
# distintos; a partir de 6 a proporcao cai e o erro de digitacao passa a ser a
# explicacao mais provavel. Transposicao nao usa este limite — ver
# `_substituicao_aceitavel`.
MIN_TOKEN_PARA_EDICAO = 6


class NivelCorrespondencia(StrEnum):
    """Quao forte e o casamento — e nao apenas um numero.

    Existe porque o analista precisa de uma classificacao acionavel, e porque o
    limiar de cada faixa e uma decisao de politica que deve ser nomeada em vez de
    ficar escondida num `if score > 0.87`.
    """

    EXATA = "exata"
    FORTE = "forte"
    PARCIAL = "parcial"
    NENHUMA = "nenhuma"


@dataclass(frozen=True, slots=True)
class Correspondencia:
    """Um casamento entre o nome consultado e uma entrada da lista.

    `tokens_casados` e `tokens_ausentes` existem para a explicabilidade que a
    decisao de KYC exige: um analista precisa ver *por que* o sistema acusou, e um
    regulador precisa poder auditar isso. Score sozinho nao explica nada.
    """

    nome_consultado: str
    nome_na_lista: str
    score: float
    nivel: NivelCorrespondencia
    tokens_casados: tuple[str, ...] = field(default=())
    tokens_ausentes: tuple[str, ...] = field(default=())
    cpf_confere: bool = False

    @property
    def exige_revisao(self) -> bool:
        return self.nivel in {
            NivelCorrespondencia.EXATA,
            NivelCorrespondencia.FORTE,
            NivelCorrespondencia.PARCIAL,
        }

    @property
    def justificativa(self) -> str:
        """Frase pronta para o parecer, no mesmo espirito do score de credito."""
        if self.cpf_confere:
            return f"CPF identico ao da entrada '{self.nome_na_lista}'"

        casados = ", ".join(self.tokens_casados) or "nenhum"
        if self.tokens_ausentes:
            return (
                f"Casamento {self.nivel.value} ({self.score:.0%}) com "
                f"'{self.nome_na_lista}': coincidem {casados}; "
                f"nao consta {', '.join(self.tokens_ausentes)}"
            )
        return (
            f"Casamento {self.nivel.value} ({self.score:.0%}) com "
            f"'{self.nome_na_lista}': coincidem {casados}"
        )


def normalizar(nome: str) -> str:
    """Remove acento, pontuacao e caixa; colapsa espaco.

    NFKD e nao NFC: decompor e descartar os combining marks e o que transforma
    "José" em "JOSE" sem tabela de substituicao manual.
    """
    sem_acento = "".join(
        c for c in unicodedata.normalize("NFKD", nome) if not unicodedata.combining(c)
    )
    limpo = "".join(c if c.isalnum() or c.isspace() else " " for c in sem_acento)
    return " ".join(limpo.upper().split())


def tokenizar(nome: str) -> tuple[str, ...]:
    """Tokens significativos, com abreviacao expandida e particula descartada."""
    tokens = []
    for bruto in normalizar(nome).split():
        if bruto in PARTICULAS:
            continue
        tokens.append(EQUIVALENCIAS.get(bruto, bruto))
    return tuple(tokens)


def _tokens_equivalentes(a: str, b: str) -> bool:
    """Se dois tokens sao a mesma palavra, tolerando abreviacao e erro de digitacao.

    ## As regras nao tratam todo "um erro de edicao" igual

    A primeira versao deste modulo aceitava qualquer distancia de edicao 1 em
    token de 4+ caracteres. A medicao derrubou isso na hora: **"Maria Silva" contra
    "Mario Silva" pontuava 0,968** — nivel praticamente identico, que e o pior
    lugar possivel para um falso positivo, porque o analista confia e aprova.

    A causa e que tres tipos de edicao tem riscos completamente diferentes:

    - **Transposicao** de caracteres adjacentes ("SILVA"/"SLIVA") e assinatura de
      digitacao. Praticamente nunca distingue duas pessoas. Sempre aceita.
    - **Insercao ou remocao** ("RODRIGUES"/"RODRIGES") tambem e digitacao, mas em
      token curto pode ser outro nome. Aceita a partir de 6 caracteres.
    - **Substituicao** e a arriscada, e em portugues especialmente **na ultima
      letra**, onde ela marca genero: MARIA/MARIO, ANTONIO/ANTONIA, CARLA/CARLO.
      Nunca aceita na posicao final; nas outras, a partir de 6 caracteres
      ("MARCOS"/"MARCUS" passa).

    Tratar os tres como "um errinho" e o que produzia o falso positivo.
    """
    if a == b:
        return True

    curto, longo = sorted((a, b), key=len)

    # Inicial abreviada de uma letra ("CARLOS E. LIMA" numa lista oficial).
    #
    # Uma inicial isolada e evidencia fraca, e por isso ela **casa** em vez de
    # contar como token ausente: ela e *consistente* com o nome completo, nao
    # prova nem refuta. O peso da decisao continua nos outros tokens — errar aqui
    # exigiria que o resto do nome tambem casasse.
    if len(curto) == 1:
        return longo.startswith(curto)

    # Prefixo truncado ("ANT." por "ANTONIO"). Tres caracteres e o minimo para
    # nao transformar prefixo em coringa.
    if len(curto) >= 3 and longo.startswith(curto):
        return True

    if len(curto) < MIN_TOKEN_PARA_FUZZY or abs(len(a) - len(b)) > 1:
        return False

    if len(a) == len(b):
        return _substituicao_aceitavel(a, b)

    return len(curto) >= MIN_TOKEN_PARA_EDICAO and _insercao_unica(curto, longo)


def _substituicao_aceitavel(a: str, b: str) -> bool:
    """Uma unica troca de caractere, com as restricoes que a medicao exigiu."""
    diferentes = [i for i, (x, y) in enumerate(zip(a, b, strict=True)) if x != y]

    # Transposicao de adjacentes: duas posicoes vizinhas com os caracteres
    # invertidos. Assinatura de digitacao, aceita em qualquer tamanho.
    if len(diferentes) == 2:
        i, j = diferentes
        return j == i + 1 and a[i] == b[j] and a[j] == b[i]

    if len(diferentes) != 1:
        return False

    posicao = diferentes[0]

    # Ultima letra: em portugues e onde o genero e marcado. MARIA/MARIO nao sao
    # a mesma pessoa, e nenhum ganho de sensibilidade justifica confundi-los.
    if posicao == len(a) - 1:
        return False

    return len(a) >= MIN_TOKEN_PARA_EDICAO


def _insercao_unica(curto: str, longo: str) -> bool:
    """Se `longo` e `curto` com um caractere a mais.

    Implementacao direta em vez de biblioteca: a pergunta e binaria e o caso e tao
    restrito que a matriz de Levenshtein seria desperdicio. Tambem evita uma
    dependencia com codigo nativo num servico que hoje nao tem nenhuma.
    """
    i = j = 0
    saltou = False
    while i < len(curto) and j < len(longo):
        if curto[i] == longo[j]:
            i += 1
            j += 1
            continue
        if saltou:
            return False
        saltou = True
        j += 1
    return True


def comparar(
    nome_consultado: str, nome_na_lista: str
) -> tuple[float, tuple[str, ...], tuple[str, ...]]:
    """Score de 0 a 1, mais os tokens que casaram e os que faltaram."""
    tokens_a = tokenizar(nome_consultado)
    tokens_b = tokenizar(nome_na_lista)

    if not tokens_a or not tokens_b:
        return 0.0, (), ()

    casados: list[str] = []
    ausentes: list[str] = []
    disponiveis = list(tokens_b)

    for token in tokens_a:
        achado = next((t for t in disponiveis if _tokens_equivalentes(token, t)), None)
        if achado is None:
            ausentes.append(token)
        else:
            casados.append(token)
            # Consome o token da lista: sem isso, um nome com "SILVA SILVA"
            # casaria duas vezes contra um unico "SILVA" e inflaria a cobertura.
            disponiveis.remove(achado)

    cobertura = len(casados) / len(tokens_a)
    caractere = SequenceMatcher(None, " ".join(tokens_a), " ".join(tokens_b)).ratio()

    score = PESO_COBERTURA * cobertura + PESO_CARACTERE * caractere

    # Penalidade por token da LISTA que o consultado nao tem. Assimetrico de
    # proposito: "JOSE SILVA" consultado contra "JOSE SILVA RODRIGUES" na lista
    # nao e a mesma pessoa com alta confianca — falta um sobrenome inteiro.
    faltando_na_consulta = [t for t in disponiveis if len(t) >= MIN_TOKEN_PARA_FUZZY]
    score -= PENALIDADE_TOKEN_AUSENTE * min(len(faltando_na_consulta), 2)

    return max(0.0, min(1.0, score)), tuple(casados), tuple(ausentes)
