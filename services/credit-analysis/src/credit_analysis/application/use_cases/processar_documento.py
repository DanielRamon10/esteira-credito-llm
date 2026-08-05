"""Caso de uso: aplicar uma extracao a analise e reavaliar o score.

Orquestra a terceira metade do fluxo de documento — envelope de injecao, piso de qualidade,
interpretacao dos campos, reavaliacao com a renda comprovada — sem implementar nenhuma dessas
etapas. Cada uma vive atras de um port ou de um modulo de infraestrutura.

## O que a Camada 8 mudou aqui

Esta classe fazia o fluxo inteiro: lia o arquivo do disco, rodava OCR, aplicava. As duas
primeiras partes sairam para `extracao_assincrona.py`, porque o OCR precisava poder rodar fora
da requisicao HTTP.

O que sobrou e a parte que **precisa do dominio de credito**: repositorio, bureau e motor de
score. E por isso que ela nao cabe numa Lambda, e por isso que a fronteira foi desenhada aqui.

Duas decisoes de seguranca ficam visiveis aqui:

1. **A renda que alimenta o score vem da extracao por regex, nunca do LLM.**
   Uma injecao de prompt no documento pode influenciar o texto que o modelo
   redige, mas nao o numero usado no calculo.
2. **Documento com extracao rejeitada nao entra no parecer.** Abaixo de 60% de
   confianca a POL-002 manda reenviar; aproveitar "o que der" seria decidir com
   base em leitura que sabemos estar errada.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import UUID

import structlog
from plataforma.seguranca import (
    ConteudoSanitizado,
    preparar_conteudo_nao_confiavel,
)

from credit_analysis.application.ports import ConsultaBureau, RepositorioAnalises
from credit_analysis.domain import scoring
from credit_analysis.domain.documento import (
    ExtracaoHolerite,
    OrigemDaRenda,
    QualidadeExtracao,
    ResultadoOCR,
)
from credit_analysis.domain.entities import AnaliseCredito, DadoExtraido, DocumentoSubmetido
from credit_analysis.domain.enums import Decisao, OrigemDado, StatusAnalise, TipoDocumento
from credit_analysis.domain.exceptions import (
    AnaliseNaoEncontrada,
    DadosInsuficientes,
)
from credit_analysis.domain.extrato import ResumoExtrato, Transacao, analisar_extrato
from credit_analysis.domain.value_objects import Dinheiro
from credit_analysis.infrastructure.ocr.extracao import extrair_holerite, extrair_transacoes

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ComandoAplicarExtracao:
    """Entrada do caso de uso.

    Recebe `documento_id` e nao `tipo`: o documento **ja esta anexado** a analise desde a
    recepcao, com tipo, nome e hash. Passar o tipo de novo abriria a possibilidade de ele
    divergir do que foi gravado, e a interpretacao (holerite ou extrato) usaria o valor errado.

    O `ocr` vem pronto. Este caso de uso nao tem motor de OCR entre as dependencias — o que
    garante, estruturalmente, que ele nao volte a rodar reconhecimento por descuido.
    """

    analise_id: UUID
    documento_id: UUID
    ocr: ResultadoOCR
    paginas_ignoradas: int = 0


@dataclass(frozen=True, slots=True)
class ResultadoProcessamento:
    """O que o processamento produziu, para a API traduzir em resposta."""

    analise: AnaliseCredito
    documento: DocumentoSubmetido
    ocr: ResultadoOCR
    extracao_holerite: ExtracaoHolerite | None = None
    resumo_extrato: ResumoExtrato | None = None
    transacoes_rejeitadas: int = 0
    conteudo: ConteudoSanitizado | None = None
    paginas_ignoradas: int = 0

    # True quando a extracao chegou para um documento ja em estado terminal, ou seja quando a
    # mensagem foi reentregue depois de o trabalho ter sido concluido.
    #
    # Campo explicito porque o trabalhador tentou inferir isso de `entrega.tentativas > 1` e
    # estava **errado**: aquilo indica retentativa (a primeira falhou de forma transitoria), nao
    # reaplicacao. Com a inferencia, toda retentativa bem-sucedida seria contada como
    # `ja_aplicada`, e a metrica que serve para detectar trabalhador morrendo antes de confirmar
    # passaria a medir outra coisa.
    reaplicacao: bool = False

    @property
    def renda_comprovada(self) -> Dinheiro | None:
        """Renda apurada, na ordem de confiabilidade das fontes."""
        if self.extracao_holerite is not None:
            return self.extracao_holerite.renda_comprovada
        if self.resumo_extrato is not None:
            # POL-005 secao 3: mediana, nao media.
            return self.resumo_extrato.renda_mediana_mensal
        return None

    @property
    def exige_revisao_humana(self) -> bool:
        """Se o caso nao pode seguir sem um analista olhar.

        Quatro gatilhos: qualidade de extracao na faixa de revisao (POL-002 secao 3.2), injecao
        detectada no documento, ausencia de renda apurada num documento que deveria conte-la, e
        **renda apurada pelo salario bruto**.

        ## O quarto gatilho, e a medicao que o produziu

        Os tres primeiros cobrem "o documento nao foi lido bem" e "o documento e hostil". O quarto
        cobre um caso que passava por confiavel: sob degradacao leve (`pouca_luz`) o Tesseract saia
        com 88,97% — acima do limiar de 85% — e a renda apurada ficava 17% acima da real
        (R$ 8.500,00 contra R$ 7.262,14), porque o liquido nao casava e a renda caia para o bruto.

        Nenhum dos tres primeiros pegava isso: a qualidade era confiavel, nao havia injecao, e havia
        renda apurada — a errada.

        Aquele caso foi corrigido na origem (o OCR escrevia `7.262 , 14` e o padrao de valor nao
        tolerava espaco em volta da virgula), mas o gatilho nao existe por causa dele: ele existe
        para quando o liquido e ilegivel de verdade, e ai a queda para o bruto e a leitura correta
        de um documento incompleto.

        A direcao do erro e o que torna o gatilho obrigatorio: renda superestimada aprova credito
        que deveria ser negado. O caminho oposto (renda subestimada) nega credito bom, o que e ruim
        de outra forma e nao coloca inadimplencia na carteira.

        ## Por que revisao e nao rejeicao

        Rejeitar o documento por um rotulo ilegivel custaria disponibilidade num documento cujo
        conteudo esta la. O bruto e um numero **verdadeiro**, so nao e o que a POL-005 manda usar —
        um analista resolve isso olhando a imagem em segundos.
        """
        if self.ocr.qualidade is not QualidadeExtracao.CONFIAVEL:
            return True
        if self.conteudo is not None and self.conteudo.suspeito:
            return True
        if self.renda_veio_do_salario_base:
            return True
        return self.renda_comprovada is None

    @property
    def renda_veio_do_salario_base(self) -> bool:
        """Se a renda apurada e o salario bruto em vez do liquido.

        Somente para holerite: no extrato a renda e a mediana dos creditos, e nao ha equivalente de
        "bruto" para confundir com ela.
        """
        if self.extracao_holerite is None:
            return False
        return self.extracao_holerite.origem_da_renda is OrigemDaRenda.BASE


class AplicarExtracao:
    """Aplica o resultado de uma extracao ao documento ja anexado, e reavalia.

    **Sem `motor_ocr` entre as dependencias**, e a ausencia e uma garantia: este caso de uso nao
    tem como rodar reconhecimento, nem por descuido numa alteracao futura. O OCR ja aconteceu.
    """

    def __init__(
        self,
        repositorio: RepositorioAnalises,
        bureau: ConsultaBureau,
    ) -> None:
        self._repositorio = repositorio
        self._bureau = bureau

    async def executar(self, comando: ComandoAplicarExtracao) -> ResultadoProcessamento:
        analise = await self._repositorio.buscar_por_id(comando.analise_id)
        if analise is None:
            raise AnaliseNaoEncontrada(f"Analise {comando.analise_id} nao encontrada")

        documento = next((d for d in analise.documentos if d.id == comando.documento_id), None)
        if documento is None:
            # Nao deveria acontecer: a recepcao anexa antes de enfileirar. Se acontecer, e erro
            # **permanente** — retentar nao faz o documento aparecer —, e por isso levanta em vez
            # de devolver a mensagem para a fila.
            raise AnaliseNaoEncontrada(
                f"Documento {comando.documento_id} nao esta na analise {comando.analise_id}"
            )

        log = logger.bind(
            analise_id=str(comando.analise_id),
            documento_id=str(comando.documento_id),
            tipo_documento=documento.tipo.value,
            arquivo=documento.nome_arquivo,
        )

        # ## Idempotencia, e ela mora aqui e nao na fila
        #
        # Entrega de mensagem e *at-least-once*: SQS reentrega, evento de S3 duplica, e o
        # trabalhador pode morrer entre aplicar e confirmar. A defesa nao e evitar a reentrega —
        # e nao ter efeito na segunda vez.
        #
        # Sem esta guarda, reaplicar uma extracao rodaria `reabrir_para_reavaliacao` e
        # incrementaria o contador de reavaliacoes por um evento que nao aconteceu, poluindo a
        # trilha de auditoria com reabertura que ninguem pediu.
        if documento.estado.terminal:
            log.info("extracao.ja_aplicada", estado=documento.estado.value)
            return self._resultado_de(analise, documento, comando, reaplicacao=True)

        documento.marcar_extraindo()

        # Envelopa e inspeciona antes de qualquer coisa tocar o texto: se ha tentativa de
        # injecao, ela deve estar registrada mesmo que o processamento falhe adiante.
        conteudo = preparar_conteudo_nao_confiavel(
            comando.ocr.texto,
            contexto={
                "analise_id": str(comando.analise_id),
                "arquivo": documento.nome_arquivo,
            },
        )

        if comando.ocr.qualidade is QualidadeExtracao.REJEITADA:
            # ## A rejeicao virou estado, e nao mais excecao
            #
            # Antes da Camada 8 isto levantava `DadosInsuficientes` e o cliente recebia 422 com a
            # instrucao de reenviar. Agora ele ja recebeu 202, e nao ha requisicao para recusar.
            #
            # A instrucao continua integral, no campo `erro` do documento, e o `GET` a devolve. O
            # que **nao** pode acontecer e a rejeicao virar silencio: o estado e terminal e
            # carrega o motivo, e o documento nao entra no parecer.
            motivo = (
                f"Qualidade da extracao insuficiente ({comando.ocr.confianca}). "
                f"Reenvie o documento com resolucao minima de 200 DPI, bordas "
                f"visiveis e sem corte de campos numericos (POL-002 secao 3.2)."
            )
            documento.rejeitar_por_qualidade(motivo, comando.ocr.confianca)
            await self._repositorio.salvar(analise)

            log.warning(
                "documento.rejeitado",
                confianca=float(comando.ocr.confianca.valor),
                motor=comando.ocr.motor,
            )
            return self._resultado_de(analise, documento, comando, conteudo=conteudo)

        extracao, resumo, rejeitadas = self._interpretar(documento.tipo, comando.ocr)
        self._registrar_dados(analise, documento, extracao, resumo, comando.ocr)

        resultado = ResultadoProcessamento(
            analise=analise,
            documento=documento,
            ocr=comando.ocr,
            extracao_holerite=extracao,
            resumo_extrato=resumo,
            transacoes_rejeitadas=len(rejeitadas),
            conteudo=conteudo,
            paginas_ignoradas=comando.paginas_ignoradas,
            # `reaplicacao` fica no default (False): este e o caminho que **fez** o trabalho. A
            # reaplicacao sai por `_resultado_de`, na guarda de estado terminal la em cima.
        )

        # A conclusao vem **depois** de montar o resultado, e nao antes: `exige_revisao_humana` e
        # propriedade dele (combina qualidade, injecao e ausencia de renda), e gravar antes
        # obrigaria a recalcular a mesma regra em dois lugares.
        documento.concluir_extracao(
            comando.ocr.texto,
            comando.ocr.confianca,
            injecao_suspeita=conteudo.suspeito,
            categorias_injecao=tuple(conteudo.categorias),
            motor_ocr=comando.ocr.motor,
            exige_revisao_humana=resultado.exige_revisao_humana,
            renda_comprovada=resultado.renda_comprovada,
            renda_origem=extracao.origem_da_renda if extracao is not None else None,
        )

        # Reavalia com a renda comprovada. E o ponto do documento existir: sem isto a extracao
        # viraria metadado decorativo e o score continuaria baseado no valor declarado pelo
        # proprio solicitante.
        parecer_anterior = analise.parecer
        await self._reavaliar(analise, resultado)

        await self._repositorio.salvar(analise)

        if parecer_anterior is not None and analise.parecer is not None:
            log.info(
                "analise.parecer_atualizado",
                score_anterior=parecer_anterior.score,
                score_novo=analise.parecer.score,
                decisao_anterior=parecer_anterior.decisao.value,
                decisao_nova=analise.parecer.decisao.value,
            )

        log.info(
            "documento.processado",
            motor=comando.ocr.motor,
            confianca=float(comando.ocr.confianca.valor),
            qualidade=comando.ocr.qualidade.value,
            renda_apurada=str(resultado.renda_comprovada) if resultado.renda_comprovada else None,
            revisao_humana=resultado.exige_revisao_humana,
            injecao_suspeita=conteudo.suspeito,
        )
        return resultado

    def _resultado_de(
        self,
        analise: AnaliseCredito,
        documento: DocumentoSubmetido,
        comando: ComandoAplicarExtracao,
        conteudo: ConteudoSanitizado | None = None,
        reaplicacao: bool = False,
    ) -> ResultadoProcessamento:
        """Resultado sem interpretacao, para os caminhos que nao chegam a reavaliar.

        Usado pela reaplicacao (estado terminal) e pela rejeicao por qualidade. Nos dois casos o
        chamador precisa de um objeto de resposta, e nos dois casos interpretar os campos seria
        trabalho jogado fora — na reaplicacao porque ja foi feito, na rejeicao porque o
        documento nao entra no parecer.
        """
        return ResultadoProcessamento(
            analise=analise,
            documento=documento,
            ocr=comando.ocr,
            conteudo=conteudo,
            paginas_ignoradas=comando.paginas_ignoradas,
            reaplicacao=reaplicacao,
        )

    async def _reavaliar(self, analise: AnaliseCredito, resultado: ResultadoProcessamento) -> None:
        """Recalcula o score com a renda comprovada e conclui a analise.

        A renda que entra aqui vem da extracao estrutural sobre o documento —
        nunca de um LLM. Uma injecao de prompt no documento nao tem por onde
        influenciar este numero.
        """
        if analise.status is not StatusAnalise.PROCESSANDO:
            analise.iniciar_processamento()

        tem_restricao = await self._bureau.tem_restricao(analise.solicitante.cpf.numero)

        entrada = scoring.EntradaScore(
            solicitante=analise.solicitante,
            proposta=analise.proposta,
            renda_comprovada=resultado.renda_comprovada,
            meses_historico_bancario=(
                resultado.resumo_extrato.meses_analisados
                if resultado.resumo_extrato is not None
                else 0
            ),
            tem_restricao_cadastral=tem_restricao,
            saldo_medio=(
                resultado.resumo_extrato.saldo_medio_mensal
                if resultado.resumo_extrato is not None
                else None
            ),
        )
        parecer = scoring.avaliar(entrada)

        # A esteira nao decide sozinha um caso que exige olhar humano: qualidade
        # de extracao na faixa de revisao, tentativa de injecao, ou renda nao
        # apurada (POL-006 secao 2).
        if resultado.exige_revisao_humana and parecer.decisao is not Decisao.NEGADO:
            parecer = replace(parecer, decisao=Decisao.ANALISE_MANUAL)

        analise.concluir(parecer)

    def _interpretar(
        self, tipo: TipoDocumento, ocr: ResultadoOCR
    ) -> tuple[ExtracaoHolerite | None, ResumoExtrato | None, list[str]]:
        """Aplica o extrator correspondente ao tipo de documento."""
        if tipo is TipoDocumento.EXTRATO_BANCARIO:
            transacoes, rejeitadas = extrair_transacoes(ocr)
            return None, self._resumir(transacoes), rejeitadas

        if tipo in {TipoDocumento.HOLERITE, TipoDocumento.IMPOSTO_RENDA}:
            return extrair_holerite(ocr), None, []

        # Comprovante de residencia e contrato social nao alimentam renda; o
        # texto fica registrado para auditoria e e isso.
        return None, None, []

    @staticmethod
    def _resumir(transacoes: list[Transacao]) -> ResumoExtrato | None:
        try:
            return analisar_extrato(transacoes)
        except DadosInsuficientes as exc:
            # Extrato curto ou ilegivel nao derruba o processamento: o documento
            # fica anexado, sem renda apurada, e o caso vai para revisao.
            logger.info("documento.extrato_nao_apuravel", motivo=str(exc))
            return None

    @staticmethod
    def _registrar_dados(
        analise: AnaliseCredito,
        documento: DocumentoSubmetido,
        extracao: ExtracaoHolerite | None,
        resumo: ResumoExtrato | None,
        ocr: ResultadoOCR,
    ) -> None:
        """Anexa os dados extraidos ao agregado, com procedencia rastreavel."""
        origem = OrigemDado.OCR if ocr.motor != "pdf:camada_texto" else OrigemDado.EXTRACAO_LLM

        if extracao is not None:
            for campo in (
                extracao.cpf,
                extracao.nome,
                extracao.empregador,
                extracao.competencia,
                extracao.salario_liquido,
            ):
                if campo is None:
                    continue
                analise.registrar_dado(
                    DadoExtraido(
                        campo=campo.nome,
                        valor=campo.valor_bruto,
                        origem=origem,
                        confianca=campo.confianca,
                        documento_id=documento.id,
                    )
                )

        if resumo is not None:
            analise.registrar_dado(
                DadoExtraido(
                    campo="renda_mediana_extrato",
                    valor=str(resumo.renda_mediana_mensal.valor),
                    origem=OrigemDado.CALCULADO,
                    confianca=ocr.confianca,
                    documento_id=documento.id,
                )
            )
            analise.registrar_dado(
                DadoExtraido(
                    campo="meses_historico_bancario",
                    valor=str(resumo.meses_analisados),
                    origem=OrigemDado.CALCULADO,
                    confianca=ocr.confianca,
                    documento_id=documento.id,
                )
            )


def _hash_arquivo(caminho: Path) -> str:
    """SHA-256 do arquivo, para deteccao de reenvio e trilha de auditoria."""
    digest = hashlib.sha256()
    with caminho.open("rb") as arquivo:
        # Le em blocos: extrato anual escaneado passa de 100MB e carregar tudo
        # em memoria por replica nao escala.
        for bloco in iter(lambda: arquivo.read(65536), b""):
            digest.update(bloco)
    return digest.hexdigest()
