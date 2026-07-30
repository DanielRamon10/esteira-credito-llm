"""Configuracao lida de variaveis de ambiente.

Prefixo `SUP_`, proprio: cada servico tem o seu namespace. Compartilhar prefixo num
monorepo cria acoplamento invisivel — mudar um valor "comum" passa a afetar dois
deploys e ninguem percebe ate o segundo quebrar.
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


class ProvedorLLM(StrEnum):
    AUTO = "auto"
    OLLAMA = "ollama"
    # Sem modelo: responde com o texto do artigo. Degrada a fluencia, nao a correcao.
    ARTIGO = "artigo"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", env_prefix="SUP_", extra="ignore"
    )

    nome_servico: str = "customer-support"
    ambiente: Ambiente = Ambiente.LOCAL
    versao: str = "0.1.0"

    # Ver a justificativa em kyc-compliance/config.py: em container este e o valor
    # correto, e o isolamento e do namespace de rede.
    host: str = "0.0.0.0"  # noqa: S104
    porta: int = 8200

    nivel_log: str = "INFO"
    log_json: bool = True

    prefixo_api: str = "/v1"
    docs_habilitados: bool = True

    diretorio_conhecimento: Path = Path("conhecimento")
    artigos_no_prompt: int = Field(default=3, ge=1, le=8)

    # `auto` tenta o Ollama e cai para o texto do artigo. Diferente do
    # `credit-analysis`, aqui NAO existe fake em producao: um assistente de
    # atendimento que responde texto sintetico ao cliente e pior que um que responde
    # o artigo cru.
    provedor_llm: ProvedorLLM = ProvedorLLM.AUTO
    ollama_endpoint: str = "http://127.0.0.1:11434"
    # Modelo pequeno de proposito: a tarefa e reescrever um artigo curto em linguagem
    # simples, nao raciocinar. `llama3.2:3b` responde em ~15s contra ~80s do 8B, e a
    # diferenca de qualidade nesta tarefa nao justifica a espera de um cliente.
    modelo_ollama: str = "llama3.2:3b"
    ollama_timeout_segundos: float = Field(default=60.0, gt=0)

    otlp_endpoint: str = ""

    @property
    def producao(self) -> bool:
        return self.ambiente is Ambiente.PROD


@lru_cache
def get_settings() -> Settings:
    return Settings()
