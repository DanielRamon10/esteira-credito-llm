"""Caso de uso: processar um documento submetido e reavaliar a analise.

Orquestra o pipeline da Camada 3 — ler arquivo, OCR com escalonamento, extrair
campos, anexar a analise, reavaliar o score com a renda comprovada — sem
implementar nenhuma dessas etapas. Cada uma vive atras de um port ou de um
modulo de infraestrutura.

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

from credit_analysis.application.ports import ConsultaBureau, MotorOCR, RepositorioAnalises
from credit_analysis.domain import scoring
from credit_analysis.domain.documento import (
    ExtracaoHolerite,
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
from credit_analysis.domain.value_objects import Dinheiro, Percentual
from credit_analysis.infrastructure.ocr import documentos as leitor
from credit_analysis.infrastructure.ocr.extracao import extrair_holerite, extrair_transacoes

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ComandoProcessarDocumento:
    """Entrada do caso de uso."""

    analise_id: UUID
    caminho: Path
    tipo: TipoDocumento


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

        Tres gatilhos: qualidade de extracao na faixa de revisao (POL-002 secao
        3.2), tentativa de injecao detectada no documento, e ausencia de renda
        apurada num documento que deveria conte-la.
        """
        if self.ocr.qualidade is not QualidadeExtracao.CONFIAVEL:
            return True
        if self.conteudo is not None and self.conteudo.suspeito:
            return True
        return self.renda_comprovada is None


class ProcessarDocumento:
    """Le, extrai e anexa um documento a uma analise existente."""

    def __init__(
        self,
        repositorio: RepositorioAnalises,
        motor_ocr: MotorOCR,
        bureau: ConsultaBureau,
    ) -> None:
        self._repositorio = repositorio
        self._motor = motor_ocr
        self._bureau = bureau

    async def executar(self, comando: ComandoProcessarDocumento) -> ResultadoProcessamento:
        analise = await self._repositorio.buscar_por_id(comando.analise_id)
        if analise is None:
            raise AnaliseNaoEncontrada(f"Analise {comando.analise_id} nao encontrada")

        log = logger.bind(
            analise_id=str(comando.analise_id),
            tipo_documento=comando.tipo.value,
            arquivo=comando.caminho.name,
        )

        carregado = leitor.carregar(comando.caminho)
        ocr = await self._obter_texto(carregado)

        # Envelopa e inspeciona antes de qualquer coisa tocar o texto: se ha
        # tentativa de injecao, ela deve estar registrada mesmo que o
        # processamento falhe adiante.
        conteudo = preparar_conteudo_nao_confiavel(
            ocr.texto,
            contexto={"analise_id": str(comando.analise_id), "arquivo": comando.caminho.name},
        )

        documento = DocumentoSubmetido(
            tipo=comando.tipo,
            nome_arquivo=comando.caminho.name,
            conteudo_hash=_hash_arquivo(comando.caminho),
            texto_extraido=ocr.texto,
            confianca_ocr=ocr.confianca,
        )

        if ocr.qualidade is QualidadeExtracao.REJEITADA:
            log.warning(
                "documento.rejeitado",
                confianca=float(ocr.confianca.valor),
                motor=ocr.motor,
            )
            raise DadosInsuficientes(
                f"Qualidade da extracao insuficiente ({ocr.confianca}). "
                f"Reenvie o documento com resolucao minima de 200 DPI, bordas "
                f"visiveis e sem corte de campos numericos (POL-002 secao 3.2)."
            )

        # Analise ja avaliada precisa ser reaberta antes de receber documento:
        # o parecer nao pode ficar descolado da evidencia que o sustenta.
        if analise.status is StatusAnalise.CONCLUIDA:
            analise.reabrir_para_reavaliacao(
                f"documento {comando.tipo.value} apresentado pelo solicitante"
            )
            log.info("analise.reaberta", reavaliacoes=analise.reavaliacoes)

        # O documento so e anexado depois de passar no piso de qualidade: a
        # analise nao deve carregar documento que a politica manda reenviar.
        analise.anexar_documento(documento)

        extracao, resumo, rejeitadas = self._interpretar(comando.tipo, ocr)
        self._registrar_dados(analise, documento, extracao, resumo, ocr)

        resultado = ResultadoProcessamento(
            analise=analise,
            documento=documento,
            ocr=ocr,
            extracao_holerite=extracao,
            resumo_extrato=resumo,
            transacoes_rejeitadas=len(rejeitadas),
            conteudo=conteudo,
            paginas_ignoradas=carregado.paginas_truncadas,
        )

        # Reavalia com a renda comprovada. E o ponto do documento existir: sem
        # isto a extracao viraria metadado decorativo e o score continuaria
        # baseado no valor declarado pelo proprio solicitante.
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
            motor=ocr.motor,
            confianca=float(ocr.confianca.valor),
            qualidade=ocr.qualidade.value,
            renda_apurada=str(resultado.renda_comprovada) if resultado.renda_comprovada else None,
            revisao_humana=resultado.exige_revisao_humana,
            injecao_suspeita=conteudo.suspeito,
        )
        return resultado

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

    async def _obter_texto(self, carregado: leitor.DocumentoCarregado) -> ResultadoOCR:
        """Usa a camada de texto do PDF quando existe; OCR so quando necessario."""
        if carregado.origem_sugerida is leitor.OrigemTexto.CAMADA_PDF:
            # Texto embutido e exato — nao ha reconhecimento envolvido, entao a
            # confianca e total. Rodar OCR aqui trocaria certeza por estimativa.
            return ResultadoOCR(
                texto=carregado.texto_embutido,
                confianca=Percentual.de(100),
                motor="pdf:camada_texto",
                palavras_reconhecidas=len(carregado.texto_embutido.split()),
                correcoes_aplicadas=("texto extraido da camada do PDF, sem OCR",),
            )

        # Multipagina: concatena o texto e usa a menor confianca das paginas. A
        # media esconderia uma pagina ilegivel no meio de um lote bom.
        textos: list[str] = []
        piores: list[Percentual] = []
        motores: set[str] = set()
        correcoes: set[str] = set()

        for pagina in carregado.paginas:
            if pagina.imagem is None:
                continue
            resultado = await self._motor.extrair(pagina.imagem)
            textos.append(resultado.texto)
            piores.append(resultado.confianca)
            motores.add(resultado.motor)
            correcoes.update(resultado.correcoes_aplicadas)

        if not textos:
            raise DadosInsuficientes("Documento sem pagina processavel")

        return ResultadoOCR(
            texto="\n\n".join(textos),
            confianca=min(piores),
            motor="+".join(sorted(motores)),
            palavras_reconhecidas=sum(len(t.split()) for t in textos),
            correcoes_aplicadas=tuple(sorted(correcoes)),
        )

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
