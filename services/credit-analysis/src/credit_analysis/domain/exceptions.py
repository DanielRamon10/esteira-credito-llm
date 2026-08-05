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


class ChaveDeIdempotenciaAusente(ErroDominio):
    """`Idempotency-Key` obrigatorio e nao enviado.

    400 e nao 428: o `draft-ietf-httpapi-idempotency-key-header` especifica 400 para chave ausente,
    e 428 Precondition Required (RFC 6585) fala de requisicao **condicional** — outra coisa.
    """

    codigo = "chave_de_idempotencia_ausente"


class ChaveDeIdempotenciaReusada(ErroDominio):
    """Mesma chave, pedido diferente.

    422 e nao 409: o conflito nao e de estado do recurso, e do **pedido** — o cliente mandou dois
    corpos distintos sob a mesma chave, e o que esta errado e o que ele enviou.

    Devolver a resposta do primeiro seria pior que qualquer erro: o cliente concluiria que submeteu
    uma analise que nao existe.
    """

    codigo = "chave_de_idempotencia_reusada"


class PedidoEmAndamento(ErroDominio):
    """A mesma chave esta sendo processada agora, por outra requisicao.

    409 com `Retry-After`: e conflito de estado, e temporario. Bloquear esperando o outro terminar
    seria a alternativa, e ela prende um worker por um tempo que nao se controla.
    """

    codigo = "pedido_em_andamento"
