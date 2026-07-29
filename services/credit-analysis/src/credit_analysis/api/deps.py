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
    ConsultaBureau,
    ModeloLinguagem,
    MotorOCR,
    RepositorioAnalises,
)
from credit_analysis.application.use_cases.analisar_credito import (
    AnalisarCredito,
    ConsultarAnalise,
    ListarAnalises,
)
from credit_analysis.application.use_cases.fundamentar_parecer import FundamentarParecer
from credit_analysis.application.use_cases.processar_documento import ProcessarDocumento
from credit_analysis.config import Settings, get_settings
from credit_analysis.domain.exceptions import RecursoIndisponivel
from credit_analysis.infrastructure.rag.retriever import RetrieverHibrido


def obter_settings() -> Settings:
    return get_settings()


def obter_repositorio(request: Request) -> RepositorioAnalises:
    repositorio: RepositorioAnalises = request.app.state.repositorio
    return repositorio


def obter_bureau(request: Request) -> ConsultaBureau:
    bureau: ConsultaBureau = request.app.state.bureau
    return bureau


def obter_caso_analisar(
    repositorio: Annotated[RepositorioAnalises, Depends(obter_repositorio)],
    bureau: Annotated[ConsultaBureau, Depends(obter_bureau)],
) -> AnalisarCredito:
    return AnalisarCredito(repositorio=repositorio, bureau=bureau)


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
    motor: Annotated[MotorOCR, Depends(obter_motor_ocr)],
    bureau: Annotated[ConsultaBureau, Depends(obter_bureau)],
) -> ProcessarDocumento:
    return ProcessarDocumento(repositorio=repositorio, motor_ocr=motor, bureau=bureau)


def obter_caso_fundamentar(
    retriever: Annotated[RetrieverHibrido, Depends(obter_retriever)],
    llm: Annotated[ModeloLinguagem, Depends(obter_llm)],
) -> FundamentarParecer:
    return FundamentarParecer(retriever=retriever, llm=llm)


# Aliases para deixar as assinaturas das rotas curtas e legiveis.
SettingsDep = Annotated[Settings, Depends(obter_settings)]
RetrieverDep = Annotated[RetrieverHibrido, Depends(obter_retriever)]
FundamentarDep = Annotated[FundamentarParecer, Depends(obter_caso_fundamentar)]
MotorOCRDep = Annotated[MotorOCR, Depends(obter_motor_ocr)]
ProcessarDocumentoDep = Annotated[ProcessarDocumento, Depends(obter_caso_processar_documento)]
AnalisarDep = Annotated[AnalisarCredito, Depends(obter_caso_analisar)]
ConsultarDep = Annotated[ConsultarAnalise, Depends(obter_caso_consultar)]
ListarDep = Annotated[ListarAnalises, Depends(obter_caso_listar)]
AgenteDep = Annotated[AgenteCredito, Depends(obter_agente)]
