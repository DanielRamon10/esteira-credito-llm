"""Wiring de dependencias."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from customer_support.application.ports import BaseDeConhecimento
from customer_support.application.use_cases.atender import Atender
from customer_support.config import Settings, get_settings


def obter_settings() -> Settings:
    return get_settings()


def obter_conhecimento(request: Request) -> BaseDeConhecimento:
    base: BaseDeConhecimento = request.app.state.conhecimento
    return base


def obter_caso_atender(request: Request) -> Atender:
    caso: Atender = request.app.state.caso_atender
    return caso


SettingsDep = Annotated[Settings, Depends(obter_settings)]
ConhecimentoDep = Annotated[BaseDeConhecimento, Depends(obter_conhecimento)]
AtenderDep = Annotated[Atender, Depends(obter_caso_atender)]
