"""Wiring de dependencias (composition root da API).

Este e o unico lugar do projeto que conhece adapters concretos. Trocar o
repositorio em memoria por Postgres e mudar uma linha aqui — nenhum caso de
uso e nenhuma rota precisam saber.

Os singletons ficam em `app.state` porque o FastAPI expoe isso via Request,
o que mantem o wiring testavel: o teste sobe a app com outros adapters e
pronto, sem monkeypatch de modulo.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from credit_analysis.application.ports import (
    AgenteCredito,
    ArmazenamentoDocumentos,
    CicloDeVidaDoDado,
    ConsultaBureau,
    ConsultaKYC,
    FilaDeTrabalho,
    ModeloLinguagem,
    MotorOCR,
    RepositorioAnalises,
)
from credit_analysis.application.use_cases.analisar_credito import (
    AnalisarCredito,
    ConsultarAnalise,
    ListarAnalises,
)
from credit_analysis.application.use_cases.extracao_assincrona import (
    ExtrairDocumento,
    ReceberDocumento,
)
from credit_analysis.application.use_cases.fundamentar_parecer import FundamentarParecer
from credit_analysis.application.use_cases.processar_documento import AplicarExtracao
from credit_analysis.config import Settings, get_settings
from credit_analysis.domain.exceptions import RecursoIndisponivel
from credit_analysis.infrastructure.rag.retriever import RetrieverHibrido


def obter_settings(request: Request) -> Settings:
    """Configuracao **do app**, com o ambiente como reserva.

    Lia `get_settings()` direto, e isso ignorava o que `criar_app(settings=...)` recebeu: um teste
    ou um segundo app no mesmo processo usariam a configuracao do ambiente, nao a injetada.

    Passou a incomodar quando a rota de documento comecou a montar o `Location` do 202 a partir de
    `prefixo_api`: com os dois divergindo, o cabecalho apontaria para um caminho que aquele app nao
    serve — e o sintoma seria 404 no polling, longe da causa.

    A reserva existe para o caminho em que nao ha app (script, `ingestao`, `__main__`).
    """
    settings: Settings | None = getattr(request.app.state, "settings", None)
    return settings or get_settings()


def obter_repositorio(request: Request) -> RepositorioAnalises:
    repositorio: RepositorioAnalises = request.app.state.repositorio
    return repositorio


def obter_bureau(request: Request) -> ConsultaBureau:
    bureau: ConsultaBureau = request.app.state.bureau
    return bureau


def obter_kyc(request: Request) -> ConsultaKYC | None:
    """Cliente de conformidade, ou None quando o gate esta desabilitado.

    Devolve None em vez de levantar 503: sem KYC a esteira ainda funciona (o gate
    nao e aplicado), e a decisao de exigi-lo pertence ao boot — `_montar_kyc` ja
    recusa subir em producao sem ele.
    """
    kyc: ConsultaKYC | None = getattr(request.app.state, "kyc", None)
    return kyc


def obter_caso_analisar(
    repositorio: Annotated[RepositorioAnalises, Depends(obter_repositorio)],
    bureau: Annotated[ConsultaBureau, Depends(obter_bureau)],
    kyc: Annotated[ConsultaKYC | None, Depends(obter_kyc)],
) -> AnalisarCredito:
    return AnalisarCredito(repositorio=repositorio, bureau=bureau, kyc=kyc)


def obter_caso_consultar(
    repositorio: Annotated[RepositorioAnalises, Depends(obter_repositorio)],
) -> ConsultarAnalise:
    return ConsultarAnalise(repositorio=repositorio)


def obter_caso_listar(
    repositorio: Annotated[RepositorioAnalises, Depends(obter_repositorio)],
) -> ListarAnalises:
    return ListarAnalises(repositorio=repositorio)


def obter_retriever(request: Request) -> RetrieverHibrido:
    """Retriever do corpus de politicas.

    Falha com 503 e mensagem acionavel em vez de 500 generico quando o indice
    nao existe: sem Postgres e sem ingestao, este endpoint nao tem como
    funcionar, e o operador precisa saber exatamente o que rodar.
    """
    retriever: RetrieverHibrido | None = getattr(request.app.state, "retriever", None)
    if retriever is None:
        raise RecursoIndisponivel(
            "Indice de politicas nao configurado. Suba o banco com "
            "`docker compose up -d`, defina CREDIT_POSTGRES_DSN e rode "
            "`python -m credit_analysis.ingestao --recriar`."
        )
    return retriever


def obter_llm(request: Request) -> ModeloLinguagem:
    llm: ModeloLinguagem = request.app.state.llm
    return llm


def obter_motor_ocr(request: Request) -> MotorOCR:
    """Cadeia de OCR configurada na aplicacao."""
    motor: MotorOCR | None = getattr(request.app.state, "motor_ocr", None)
    if motor is None:
        raise RecursoIndisponivel(
            "Nenhum motor de OCR disponivel. Instale o Tesseract "
            "(`winget install UB-Mannheim.TesseractOCR`) ou configure "
            "CREDIT_ANTHROPIC_API_KEY para usar o modelo de visao."
        )
    return motor


def obter_agente(request: Request) -> AgenteCredito:
    """Agente configurado na aplicacao.

    503 e nao fake: um agente falso responderia com trilha vazia e texto
    plausivel, e quem consome nao teria como saber que nenhuma ferramenta rodou.
    Indisponibilidade honesta e melhor que disponibilidade fingida.
    """
    agente: AgenteCredito | None = getattr(request.app.state, "agente", None)
    if agente is None:
        raise RecursoIndisponivel(
            "Agente indisponivel: nenhum modelo com suporte a ferramentas. "
            "Instale o Ollama (`winget install Ollama.Ollama`) e rode "
            "`ollama pull qwen2.5:7b`, ou configure CREDIT_ANTHROPIC_API_KEY."
        )
    return agente


def obter_caso_processar_documento(
    repositorio: Annotated[RepositorioAnalises, Depends(obter_repositorio)],
    bureau: Annotated[ConsultaBureau, Depends(obter_bureau)],
) -> AplicarExtracao:
    return AplicarExtracao(repositorio=repositorio, bureau=bureau)


def obter_armazenamento(request: Request) -> ArmazenamentoDocumentos:
    armazenamento: ArmazenamentoDocumentos = request.app.state.armazenamento
    return armazenamento


def obter_fila(request: Request) -> FilaDeTrabalho:
    fila: FilaDeTrabalho = request.app.state.fila
    return fila


def obter_caso_receber_documento(
    repositorio: Annotated[RepositorioAnalises, Depends(obter_repositorio)],
    armazenamento: Annotated[ArmazenamentoDocumentos, Depends(obter_armazenamento)],
    fila: Annotated[FilaDeTrabalho, Depends(obter_fila)],
) -> ReceberDocumento:
    return ReceberDocumento(repositorio=repositorio, armazenamento=armazenamento, fila=fila)


def obter_caso_extrair(
    armazenamento: Annotated[ArmazenamentoDocumentos, Depends(obter_armazenamento)],
    motor: Annotated[MotorOCR, Depends(obter_motor_ocr)],
) -> ExtrairDocumento:
    return ExtrairDocumento(armazenamento=armazenamento, motor_ocr=motor)


def obter_caso_fundamentar(
    retriever: Annotated[RetrieverHibrido, Depends(obter_retriever)],
    llm: Annotated[ModeloLinguagem, Depends(obter_llm)],
) -> FundamentarParecer:
    return FundamentarParecer(retriever=retriever, llm=llm)


# Aliases para deixar as assinaturas das rotas curtas e legiveis.
SettingsDep = Annotated[Settings, Depends(obter_settings)]


def obter_ciclo_de_vida(request: Request) -> CicloDeVidaDoDado:
    """Repositorio, quando ele sabe cuidar de retencao e apagamento.

    ## 503 e nao 501, e a escolha e sobre taxonomia

    `501 Not Implemented` seria tentador — o adapter em memoria nunca vai suportar isto. Mas o que
    decide se a capacidade existe e **configuracao**: com `CREDIT_POSTGRES_DSN`, o repositorio
    montado a oferece. Isso e a mesma classe do OCR ausente, que este projeto ja responde com 503 e
    uma instrucao de como habilitar.

    Inventar um codigo novo para uma distincao que nao se sustenta faria o cliente tratar dois casos
    identicos de formas diferentes.

    ## Por que a checagem e por capacidade e nao por configuracao

    Conferir `settings.usar_pgvector` diria qual repositorio **deveria** estar montado. O
    `isinstance` diz qual esta. A diferenca aparece em teste, onde o repositorio e injetado e a
    configuracao nao descreve o que roda — e um 503 mentiroso ali levaria meia hora para ser
    entendido.
    """
    repositorio = obter_repositorio(request)
    if not isinstance(repositorio, CicloDeVidaDoDado):
        raise RecursoIndisponivel(
            "Este ambiente nao conserva dado de forma duravel, portanto nao ha o que apagar a "
            "pedido: o repositorio em memoria perde tudo no restart. Configure "
            "CREDIT_POSTGRES_DSN para habilitar o atendimento de pedidos do titular."
        )
    return repositorio


RepositorioDep = Annotated[RepositorioAnalises, Depends(obter_repositorio)]
CicloDeVidaDep = Annotated[CicloDeVidaDoDado, Depends(obter_ciclo_de_vida)]
RetrieverDep = Annotated[RetrieverHibrido, Depends(obter_retriever)]
FundamentarDep = Annotated[FundamentarParecer, Depends(obter_caso_fundamentar)]
MotorOCRDep = Annotated[MotorOCR, Depends(obter_motor_ocr)]
AplicarExtracaoDep = Annotated[AplicarExtracao, Depends(obter_caso_processar_documento)]
ReceberDocumentoDep = Annotated[ReceberDocumento, Depends(obter_caso_receber_documento)]
ExtrairDocumentoDep = Annotated[ExtrairDocumento, Depends(obter_caso_extrair)]
AnalisarDep = Annotated[AnalisarCredito, Depends(obter_caso_analisar)]
ConsultarDep = Annotated[ConsultarAnalise, Depends(obter_caso_consultar)]
ListarDep = Annotated[ListarAnalises, Depends(obter_caso_listar)]
AgenteDep = Annotated[AgenteCredito, Depends(obter_agente)]
