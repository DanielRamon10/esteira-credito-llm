"""Wiring de dependencias (composition root da API)."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from kyc_compliance.application.ports import RepositorioListas, RepositorioTriagens
from kyc_compliance.application.use_cases.triar_cliente import (
    ConsultarTriagem,
    ListarTriagens,
    TriarCliente,
)
from kyc_compliance.config import Settings, get_settings


def obter_settings() -> Settings:
    return get_settings()


def obter_listas(request: Request) -> RepositorioListas:
    listas: RepositorioListas = request.app.state.listas
    return listas


def obter_repositorio(request: Request) -> RepositorioTriagens:
    repositorio: RepositorioTriagens = request.app.state.repositorio
    return repositorio


def obter_caso_triar(
    listas: Annotated[RepositorioListas, Depends(obter_listas)],
    repositorio: Annotated[RepositorioTriagens, Depends(obter_repositorio)],
) -> TriarCliente:
    return TriarCliente(listas=listas, repositorio=repositorio)


def obter_caso_consultar(
    repositorio: Annotated[RepositorioTriagens, Depends(obter_repositorio)],
) -> ConsultarTriagem:
    return ConsultarTriagem(repositorio=repositorio)


def obter_caso_listar(
    repositorio: Annotated[RepositorioTriagens, Depends(obter_repositorio)],
) -> ListarTriagens:
    return ListarTriagens(repositorio=repositorio)


SettingsDep = Annotated[Settings, Depends(obter_settings)]
ListasDep = Annotated[RepositorioListas, Depends(obter_listas)]
TriarDep = Annotated[TriarCliente, Depends(obter_caso_triar)]
ConsultarDep = Annotated[ConsultarTriagem, Depends(obter_caso_consultar)]
ListarDep = Annotated[ListarTriagens, Depends(obter_caso_listar)]
