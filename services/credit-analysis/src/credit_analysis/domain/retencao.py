"""Politica de ciclo de vida do dado pessoal.

## Por que isto e dominio, e nao um cron

Prazo de retencao e **regra de negocio com base legal**, e nao detalhe de agendamento. Quem decide
que a decisao de credito fica 5 anos e a POL-006 §5 apoiada na Resolucao CMN 4.658; quem decide que
o texto de OCR sai antes disso e a LGPD art. 15, que manda guardar dado pessoal so pelo tempo
necessario a finalidade.

Deixar esses prazos num script de purga faria a politica mudar quando alguem ajustasse o script, e
sem revisao de quem responde por ela. Aqui eles ficam nomeados, com o artigo que os sustenta, e o
script apenas os aplica.

## A tensao que este modulo resolve

Duas obrigacoes em direcoes opostas, e as duas sao lei:

- **LGPD art. 18 §VI** da ao titular o direito de exclusao dos dados dele;
- **LGPD art. 16 §I** permite — e a regulacao bancaria **exige** — conservar o que for necessario
  para cumprir obrigacao legal. Um parecer de credito e registro de decisao: apagar o score e a
  justificativa deixaria o banco sem como responder a um questionamento do proprio titular (art.
  20) ou do regulador.

Atender so a primeira destroi a trilha; atender so a segunda ignora o direito. A saida e separar
**identificacao** de **decisao**: os identificadores saem, o registro da decisao fica.

## Uma palavra que este modulo evita: "anonimizacao"

Seria comodo chamar o resultado de anonimizado — dado anonimizado sai do escopo da LGPD (art. 12), o
que e conveniente demais para ser aceito sem conferir.

Nao e anonimizacao por duas razoes:

1. **Hash de CPF nao anonimiza.** O espaco e de 10^11 combinacoes, e o digito verificador reduz
   para ~10^9 validas. Uma GPU percorre isso em segundos: dado o hash, o CPF volta. Pseudonimo
   derivado de identificador com dominio pequeno e reversivel por construcao;
2. **o identificador da analise permanece**, porque a trilha de auditoria precisa dele. Quem tiver
   um mapeamento antigo `analise_id -> CPF` — no sistema de origem, num log de acesso —
   re-identifica.

Ou seja: o que este modulo produz e **retencao sob base legal com identificadores removidos**, que
continua sendo dado pessoal e continua sob a LGPD. Chamar de anonimizacao seria uma alegacao que o
desenho nao sustenta.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

# ------------------------------------------------------------------ Prazos

# Registro da decisao de credito: 5 anos.
#
# POL-006 §5, alinhada a Resolucao CMN 4.658 e ao prazo prescricional do Codigo Civil art. 206 §5
# para divida liquida. E o prazo em que o titular ainda pode questionar a decisao e o banco precisa
# poder mostrar em que se baseou.
RETENCAO_DECISAO = timedelta(days=365 * 5)

# Texto extraido por OCR: 90 dias.
#
# ## Por que muito menos que a decisao
#
# O texto **nao e** o registro da decisao — ele e uma copia de trabalho. O que sustenta o parecer
# esta persistido em outro lugar e sobrevive a purga:
#
# - `parecer.justificativas` e `parecer.politicas_aplicadas`, a fundamentacao em texto;
# - `dados_extraidos`, campo por campo, com origem e confianca;
# - `documento.renda_comprovada` e `documento.renda_origem`, o numero que alimentou o score;
# - `documento.referencia`, que aponta para o objeto original com versao.
#
# Guardar o texto inteiro por 5 anos seria manter nome, empregador, CPF e salario em claro numa
# coluna consultavel, pelo tempo de uma obrigacao que ele nao cumpre. E o oposto do art. 15.
#
# ## Por que 90 dias e nao zero
#
# Reprocessamento. Uma extracao pode ser reaplicada — reentrega de fila, correcao de parser, revisao
# aberta pelo titular — e nesse periodo ter o texto evita reprocessar o objeto. Passados 90 dias, o
# caminho de reprocessamento e reler o objeto no armazenamento, que e mais caro e continua possivel.
RETENCAO_TEXTO_OCR = timedelta(days=90)

# Objeto original no armazenamento: 365 dias, e **nao e este modulo que aplica**.
#
# Quem expira e a regra de ciclo de vida do bucket (`infra/terraform/main.tf`), de proposito: a
# aplicacao nao tem `s3:DeleteObject`, entao um comprometimento dela nao apaga a evidencia de um
# parecer. O valor esta aqui para o prazo ser legivel junto dos outros, e a duplicacao e anotada nos
# dois lados.
#
# **Limitacao conhecida, e ela e real:** um pedido de exclusao (art. 18) nao consegue remover o
# objeto na hora por este caminho — ele sai quando o ciclo de vida alcanca. 365 dias de espera nao e
# atendimento ao direito. O desenho para fechar isso e uma role separada para o job de purga, com
# `DeleteObject` restrito ao prefixo; ver `infra/terraform/retencao.tf`.
RETENCAO_OBJETO = timedelta(days=365)


class ClasseDeDado(StrEnum):
    """Para que cada classe de dado existe, que e o que define quanto tempo ela fica.

    Existe como enum e nao como comentario porque o log de purga precisa dizer **o que** foi
    removido, e "removi 412 linhas" nao e resposta auditavel.
    """

    # Identifica uma pessoa: nome, CPF, data de nascimento, renda declarada.
    IDENTIFICACAO = "identificacao"
    # Conteudo do documento enviado, em texto.
    TEXTO_DOCUMENTO = "texto_documento"
    # Score, decisao, justificativa, politicas. O registro que a obrigacao legal exige.
    DECISAO = "decisao"


@dataclass(frozen=True, slots=True)
class Prazo:
    """Um prazo com a base legal que o sustenta, para o log de purga poder cita-la."""

    classe: ClasseDeDado
    duracao: timedelta
    base_legal: str

    def vencido_em(self, referencia: datetime, *, agora: datetime | None = None) -> bool:
        """Se o prazo desta classe ja passou, contado da data de referencia.

        `agora` injetavel porque o teste precisa de tempo controlado: verificar retencao de 5 anos
        esperando 5 anos nao e teste, e `freeze_time` esconderia a dependencia num decorator.
        """
        momento = agora if agora is not None else datetime.now(UTC)
        return momento - referencia >= self.duracao


PRAZOS: dict[ClasseDeDado, Prazo] = {
    ClasseDeDado.TEXTO_DOCUMENTO: Prazo(
        classe=ClasseDeDado.TEXTO_DOCUMENTO,
        duracao=RETENCAO_TEXTO_OCR,
        base_legal="LGPD art. 15 §I (fim da necessidade); copia de trabalho, nao registro",
    ),
    ClasseDeDado.IDENTIFICACAO: Prazo(
        classe=ClasseDeDado.IDENTIFICACAO,
        duracao=RETENCAO_DECISAO,
        base_legal="POL-006 §5 / CMN 4.658; apos o prazo, LGPD art. 15 §I",
    ),
    ClasseDeDado.DECISAO: Prazo(
        classe=ClasseDeDado.DECISAO,
        # A decisao **nao expira** por este caminho. O prazo existe para o registro ficar
        # comparavel aos outros, e a purga nunca a remove: quem decide descartar registro de
        # decisao e a area de compliance, num processo que nao e um job noturno.
        duracao=RETENCAO_DECISAO,
        base_legal="POL-006 §5; conservado sob LGPD art. 16 §I",
    ),
}


def texto_pode_ser_purgado(atualizada_em: datetime, *, agora: datetime | None = None) -> bool:
    """Se o texto de OCR desta analise passou do prazo.

    Conta de `atualizada_em` e nao de `criada_em`: uma analise reaberta por reavaliacao volta a ter
    finalidade, e a contagem recomeca. Usar a criacao purgaria o texto de um caso em andamento que
    nasceu ha muito tempo — situacao comum quando o cliente demora a enviar documento.
    """
    return PRAZOS[ClasseDeDado.TEXTO_DOCUMENTO].vencido_em(atualizada_em, agora=agora)


def identificacao_pode_ser_removida(criada_em: datetime, *, agora: datetime | None = None) -> bool:
    """Se os identificadores desta analise passaram do prazo de conservacao.

    Aqui a contagem e de `criada_em`, e a diferenca com a funcao acima e deliberada: o prazo de
    conservacao existe para responder por uma decisao tomada numa data, e essa data nao se move
    porque houve reavaliacao. Contar de `atualizada_em` deixaria uma analise mexida perto do fim do
    prazo conservar identificacao por mais 5 anos.
    """
    return PRAZOS[ClasseDeDado.IDENTIFICACAO].vencido_em(criada_em, agora=agora)
