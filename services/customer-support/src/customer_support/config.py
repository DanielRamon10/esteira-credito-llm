"""Configuracao lida de variaveis de ambiente.

Prefixo `SUP_`, proprio: cada servico tem o seu namespace. Compartilhar prefixo num
monorepo cria acoplamento invisivel — mudar um valor "comum" passa a afetar dois
deploys e ninguem percebe ate o segundo quebrar.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
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

    # --- Autenticacao (Camada 7) ---
    #
    # **Nao existe `auth_habilitado`.** Autenticacao que se desliga por variavel de ambiente
    # e autenticacao que vai estar desligada em algum ambiente, por um motivo temporario que
    # ninguem reverteu, sem nada falhando para avisar.
    #
    # As duas fontes de chave sao mutuamente exclusivas e uma e obrigatoria — ver o validador
    # no fim desta classe.
    auth_emissor: str = ""
    auth_audiencia: str = "customer-support"

    # PEM da chave **publica**, para desenvolvimento e ambientes sem IdP com JWKS.
    auth_chave_publica: str = ""

    # JWKS do IdP. Preferivel em producao: permite rotacao de chave sem redeploy, porque o
    # emissor publica a nova e os servicos a veem no proximo ciclo de cache.
    auth_jwks_url: str = ""

    # Caminho de um arquivo com a chave publica. **E esta a forma usada pelo compose e pelo
    # Kubernetes**, e nao a variavel com o PEM inline.
    #
    # Duas razoes concretas:
    #
    # - PEM em ConfigMap ou em variavel de ambiente aparece inteiro num `kubectl describe pod`
    #   e num `docker inspect`. A chave e publica, entao nao e vazamento — mas o mesmo caminho
    #   seria usado para material sensivel por analogia, e o habito importa;
    # - arquivo e o que permite **rotacao sem recriar o pod**: o kubelet atualiza um Secret
    #   montado como volume em segundos, enquanto variavel de ambiente e fixada na criacao do
    #   container. Com JWKS a rotacao e ainda melhor; com arquivo, ela ao menos e possivel.
    #
    # Lido no boot, nao por requisicao: um arquivo ausente precisa impedir a subida, e reler a
    # cada requisicao poria I/O de disco no caminho de toda chamada.
    auth_chave_publica_arquivo: Path | None = None

    otlp_endpoint: str = ""

    @model_validator(mode="after")
    def _conferir_fonte_de_chave(self) -> Settings:
        """Exige **exatamente uma** fonte de chave de verificacao.

        Nenhuma: o servico nao sabe verificar token, e falhar aqui e melhor que recusar tudo
        (indisponivel) ou aceitar tudo (aberto). As duas: ambiguidade sobre qual manda, e uma
        configuracao antiga esquecida numa delas aceitaria token que deveria ser rejeitado.
        """
        tem_pem = bool(self.auth_chave_publica.strip())
        tem_jwks = bool(self.auth_jwks_url.strip())
        tem_arquivo = self.auth_chave_publica_arquivo is not None

        # Exatamente **uma** das tres. `sum` sobre booleanos em vez de uma cadeia de `if`: com
        # tres fontes, a cadeia tem seis combinacoes e uma delas escapa por descuido.
        fontes = sum((tem_pem, tem_jwks, tem_arquivo))
        if fontes > 1:
            raise ValueError(
                "informe **uma** fonte de chave: SUP_AUTH_CHAVE_PUBLICA, "
                "SUP_AUTH_CHAVE_PUBLICA_ARQUIVO ou SUP_AUTH_JWKS_URL. "
                "Com mais de uma, qual delas valida fica ambiguo, e uma configuracao antiga "
                "esquecida numa aceitaria token que deveria ser rejeitado."
            )

        if fontes == 0:
            raise ValueError(
                "autenticacao exige uma fonte de chave: "
                "SUP_AUTH_CHAVE_PUBLICA_ARQUIVO, SUP_AUTH_CHAVE_PUBLICA "
                "ou SUP_AUTH_JWKS_URL.\n"
                "Para desenvolvimento, sem conta em provedor nenhum:\n"
                "  python -m plataforma.emissor_local gerar-chaves\n"
                "  export SUP_AUTH_CHAVE_PUBLICA_ARQUIVO=.chaves/publica.pem\n"
                "  export SUP_AUTH_EMISSOR=https://local.esteira-credito.invalid\n"
                "Nao ha modo desligado, e a ausencia e deliberada: ver api/seguranca.py."
            )
        if not self.auth_emissor.strip():
            # Sem emissor, o PyJWT compararia `iss` contra "" e nenhum token passaria — o
            # sintoma (401 universal) nao aponta para configuracao ausente.
            raise ValueError("SUP_AUTH_EMISSOR e obrigatorio")
        return self

    @property
    def producao(self) -> bool:
        return self.ambiente is Ambiente.PROD


@lru_cache
def get_settings() -> Settings:
    return Settings()
