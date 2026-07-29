"""Configuracao lida de variaveis de ambiente.

Prefixo `KYC_` e nao `CREDIT_`: cada servico tem o proprio namespace de
configuracao. Compartilhar prefixo entre servicos num monorepo parece conveniente
e cria um acoplamento invisivel — mudar um valor "comum" passa a afetar dois
deploys, e ninguem percebe ate o segundo quebrar.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Ambiente(StrEnum):
    LOCAL = "local"
    STAGING = "staging"
    PROD = "prod"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="KYC_",
        extra="ignore",
    )

    nome_servico: str = "kyc-compliance"
    ambiente: Ambiente = Ambiente.LOCAL
    versao: str = "0.1.0"

    # `0.0.0.0` e o valor correto num container, e o `noqa` documenta por que.
    #
    # A regra S104 do bandit existe para o caso de um processo em maquina
    # compartilhada expondo servico sem intencao. Em container a situacao e o
    # inverso: bind em `127.0.0.1` torna o processo inalcancavel de fora do
    # namespace de rede, e o servico simplesmente nao recebe trafego. O isolamento
    # aqui e feito pelo namespace e pela NetworkPolicy (ver infra/k8s), nao pelo
    # endereco de bind.
    host: str = "0.0.0.0"  # noqa: S104
    porta: int = 8100

    nivel_log: str = "INFO"
    log_json: bool = True

    prefixo_api: str = "/v1"
    docs_habilitados: bool = True

    # Diretorio das listas restritivas, relativo a raiz do servico.
    #
    # Nao ha fallback para lista vazia: `ListasDeArquivo` levanta erro se o
    # diretorio nao existir. Servico de conformidade que sobe sem lista aprova
    # todo mundo — degradacao na direcao mais perigosa possivel.
    diretorio_listas: Path = Path("dados/listas")

    otlp_endpoint: str = ""
    trace_amostragem: float = Field(default=1.0, ge=0.0, le=1.0)

    @property
    def producao(self) -> bool:
        return self.ambiente is Ambiente.PROD


@lru_cache
def get_settings() -> Settings:
    return Settings()
