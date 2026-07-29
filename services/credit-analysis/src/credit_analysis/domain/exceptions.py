"""Excecoes do dominio.

Todas herdam de ErroDominio para que a camada de API consiga traduzir
qualquer violacao de regra de negocio em uma resposta HTTP coerente sem
precisar conhecer cada subclasse.
"""


class ErroDominio(Exception):
    """Raiz da hierarquia de erros de negocio."""

    codigo = "erro_dominio"


class ValorInvalido(ErroDominio):
    """Um value object recebeu entrada que viola sua invariante."""

    codigo = "valor_invalido"


class MoedasIncompativeis(ValorInvalido):
    """Tentativa de operar sobre valores em moedas diferentes."""

    codigo = "moedas_incompativeis"


class TransicaoInvalida(ErroDominio):
    """Transicao de estado nao permitida pela maquina de estados da analise."""

    codigo = "transicao_invalida"


class AnaliseNaoEncontrada(ErroDominio):
    """Nenhuma analise corresponde ao identificador informado."""

    codigo = "analise_nao_encontrada"


class DadosInsuficientes(ErroDominio):
    """Nao ha dados minimos para produzir um parecer confiavel."""

    codigo = "dados_insuficientes"


class RecursoIndisponivel(ErroDominio):
    """Uma capacidade opcional nao esta configurada neste ambiente.

    Diferente de falha: o servico esta saudavel, apenas sem aquele recurso
    (indice de politicas, por exemplo). Vira 503 com instrucao de como
    habilitar, nao 500.
    """

    codigo = "recurso_indisponivel"
