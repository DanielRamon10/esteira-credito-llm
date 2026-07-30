"""Configuracao da aplicacao.

Tudo que muda entre ambientes vem de variavel de ambiente (12-factor). Nada
de `if ambiente == "prod"` espalhado pelo codigo — o container recebe as
variaveis certas e o mesmo binario roda em qualquer lugar.

`Settings` e cacheada: ler o ambiente uma vez e reusar evita divergencia se
alguem mexer em os.environ no meio da execucao.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProvedorLLM(StrEnum):
    """Qual adapter de LLM usar."""

    AUTO = "auto"
    ANTHROPIC = "anthropic"
    OLLAMA = "ollama"
    FAKE = "fake"


class Ambiente(StrEnum):
    LOCAL = "local"
    DEV = "dev"
    HOMOLOG = "homolog"
    PROD = "prod"


class Settings(BaseSettings):
    """Configuracao lida de variaveis de ambiente e do arquivo .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="CREDIT_",
        extra="ignore",
    )

    # --- Aplicacao ---
    nome_servico: str = "credit-analysis"
    ambiente: Ambiente = Ambiente.LOCAL
    versao: str = "0.1.0"
    debug: bool = False

    # --- API ---
    # `0.0.0.0` e o valor correto num container: bind em `127.0.0.1` deixaria o
    # processo inalcancavel de fora do namespace de rede. O isolamento e feito pelo
    # namespace e pela NetworkPolicy (infra/k8s), nao pelo endereco de bind.
    host: str = "0.0.0.0"  # noqa: S104
    porta: int = 8000
    prefixo_api: str = "/v1"

    # --- Logging ---
    nivel_log: str = "INFO"
    log_json: bool = True

    # --- Regras de negocio parametrizaveis ---
    # Ficam aqui, e nao hardcoded, porque o time de risco ajusta sem deploy.
    taxa_juros_padrao_mensal: float = Field(default=1.99, ge=0, le=20)
    prazo_maximo_meses: int = Field(default=120, ge=1, le=480)

    # --- Bureau ---
    bureau_habilitado: bool = True
    bureau_timeout_segundos: float = Field(default=3.0, gt=0)

    # --- RAG ---
    # Diretorio do corpus de politicas, relativo a raiz do servico.
    diretorio_politicas: Path = Path("politicas")
    modelo_embedding: str = "intfloat/multilingual-e5-large"
    # Vazio = usa o vector store em memoria. Preencher aponta para pgvector.
    postgres_dsn: str = ""
    trechos_por_consulta: int = Field(default=5, ge=1, le=20)

    # --- LLM ---
    # `auto` escolhe na ordem: Anthropic (se houver chave) -> Ollama (se o
    # daemon responder) -> fake. Nenhuma das tres exige configuracao: o
    # servico sobe em qualquer ambiente e degrada de forma explicita.
    provedor_llm: ProvedorLLM = ProvedorLLM.AUTO

    # Anthropic (opcional, pago).
    anthropic_api_key: str = ""
    modelo_llm: str = "claude-opus-5"

    # Ollama (local, gratuito). Modelo escolhido por medicao — ver o cabecalho
    # de `infrastructure/llm/ollama_adapter.py`.
    ollama_endpoint: str = "http://127.0.0.1:11434"
    modelo_ollama: str = "llama3.1:8b"

    llm_timeout_segundos: float = Field(default=60.0, gt=0)
    ollama_timeout_segundos: float = Field(default=240.0, gt=0)

    # --- Agente (Camada 4) ---
    # Modelo **diferente** do usado na fundamentacao, e nao por descuido. Medido
    # com 9 cenarios: `qwen2.5:7b` abstem-se corretamente 4/4 quando a pergunta
    # nao exige ferramenta; `llama3.1:8b` abstem-se 0/4, chamando ferramenta
    # ate para "Bom dia". Na fundamentacao a ordem se inverte. Ver o cabecalho de
    # `infrastructure/agente/grafo.py`.
    modelo_agente: str = "qwen2.5:7b"

    # Teto de execucoes de ferramenta por atendimento — a protecao contra o
    # modelo que nao sabe parar de chamar ferramenta.
    agente_max_passos: int = Field(default=6, ge=1, le=20)

    # Orcamento de tempo do atendimento inteiro, nao de uma chamada ao modelo.
    agente_timeout_segundos: float = Field(default=180.0, gt=0)

    # --- KYC (servico de conformidade) ---
    # Vazio desliga o gate de conformidade. Em `prod` isso e ERRO de subida, nao
    # degradacao: aprovar credito sem triagem de lista restritiva e descumprir a
    # Circular BCB 3.978, e um servico que faz isso em silencio e pior que um
    # servico fora do ar. Ver `_montar_kyc` em `api/app.py`.
    kyc_url: str = ""

    # Credencial de servico para chamar o KYC, que a partir da Camada 7 exige token.
    #
    # O token deste servico (`aud=credit-analysis`) **nao pode ser repassado** ao KYC: seria a
    # escalada lateral que a validacao de audiencia existe para impedir. Ele precisa de
    # credencial propria, via `client_credentials`.
    #
    # Duas formas, e a escolha e explicita — nunca `auto`. Um fallback do IdP para o token
    # estatico transformaria indisponibilidade do IdP em uso de credencial possivelmente
    # expirada, e o sintoma seria 401 intermitente vindo do KYC.
    kyc_token_url: str = ""
    kyc_client_id: str = ""
    kyc_client_secret: str = ""

    # Token pronto, para desenvolvimento e para o compose: o projeto roda sem conta em provedor
    # nenhum, e nao ha IdP local com endpoint de token. Emitido por
    # `python -m plataforma.emissor_local token --audiencia kyc-compliance`.
    kyc_token: str = ""

    # Timeout por tentativa. A triagem do outro servico e comparacao em memoria; passar de 3s
    # indica rede ou saturacao, nao calculo.
    kyc_timeout_segundos: float = Field(default=3.0, gt=0)
    kyc_tentativas: int = Field(default=2, ge=1, le=5)

    # --- Autenticacao (Camada 7) ---
    #
    # **Nao existe `auth_habilitado`.** Autenticacao que se desliga por variavel de
    # ambiente e autenticacao que vai estar desligada em algum ambiente, por um motivo
    # temporario que ninguem reverteu, sem nada falhando para avisar.
    #
    # As duas fontes de chave sao mutuamente exclusivas e uma delas e obrigatoria — ver o
    # validador no fim desta classe. Ausencia das duas e erro de subida.
    auth_emissor: str = ""
    auth_audiencia: str = "credit-analysis"

    # PEM da chave **publica**. Para desenvolvimento e para ambientes sem IdP com JWKS.
    auth_chave_publica: str = ""

    # URL do JWKS do IdP. Preferivel em producao: permite **rotacao de chave sem
    # redeploy**, porque o emissor publica a nova e os servicos a veem no proximo ciclo
    # de cache. Com PEM em variavel de ambiente, rotacionar exige reiniciar os tres
    # servicos ao mesmo tempo.
    auth_jwks_url: str = ""

    # --- Observabilidade (Camada 5) ---
    # Vazio desliga o tracing. Observabilidade nunca deve impedir o servico de
    # subir: uma falha no coletor de traces viraria indisponibilidade da esteira
    # de credito, o que troca um problema pequeno por um grande.
    otlp_endpoint: str = ""

    # 100% de proposito. Uma esteira de credito faz poucas requisicoes caras
    # (~80s), nao milhoes baratas — amostrar economizaria pouco e jogaria fora o
    # trace que alguem vai querer investigar. Existe para o dia em que o volume
    # mudar essa conta.
    trace_amostragem: float = Field(default=1.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _conferir_fonte_de_chave(self) -> Settings:
        """Exige **exatamente uma** fonte de chave de verificacao.

        Tres estados possiveis, e dois deles sao erro:

        - nenhuma: o servico nao sabe verificar token. Falhar aqui e melhor que as duas
          alternativas — recusar tudo (indisponivel) ou aceitar tudo (aberto);
        - as duas: ambiguidade sobre qual chave manda. Uma configuracao antiga esquecida
          numa delas aceitaria token que deveria ser rejeitado, e o `/health` diria "ok".

        Em `local` o erro traz o comando exato. `FileNotFoundError` num PEM vazio mandaria
        quem esta configurando procurar no codigo o que gera aquele arquivo.
        """
        tem_pem = bool(self.auth_chave_publica.strip())
        tem_jwks = bool(self.auth_jwks_url.strip())

        if tem_pem and tem_jwks:
            raise ValueError(
                "CREDIT_AUTH_CHAVE_PUBLICA e CREDIT_AUTH_JWKS_URL sao mutuamente "
                "exclusivas: defina apenas uma."
            )
        if not tem_pem and not tem_jwks:
            # Mensagem com os comandos exatos. Um `ValidationError` seco mandaria quem esta
            # configurando procurar no codigo o que preenche estas variaveis — e a resposta
            # esta em outro pacote (`plataforma.emissor_local`), o que torna a busca pior.
            raise ValueError(
                "autenticacao exige CREDIT_AUTH_CHAVE_PUBLICA ou CREDIT_AUTH_JWKS_URL.\n"
                "Para desenvolvimento, sem conta em provedor nenhum:\n"
                "  python -m plataforma.emissor_local gerar-chaves\n"
                '  export CREDIT_AUTH_CHAVE_PUBLICA="$(cat .chaves/publica.pem)"\n'
                "  export CREDIT_AUTH_EMISSOR=https://local.esteira-credito.invalid\n"
                "Nao ha modo desligado, e a ausencia e deliberada: ver api/seguranca.py."
            )
        if not self.auth_emissor.strip():
            # Sem emissor, `verificar` receberia string vazia e o PyJWT compararia `iss`
            # contra "" — nenhum token passaria, e o sintoma (403/401 universal) nao aponta
            # para configuracao ausente.
            raise ValueError("CREDIT_AUTH_EMISSOR e obrigatorio")
        return self

    @property
    def usar_pgvector(self) -> bool:
        return bool(self.postgres_dsn.strip())

    @property
    def usar_llm_real(self) -> bool:
        """Se ha credencial Anthropic — habilita tambem o OCR por visao."""
        return bool(self.anthropic_api_key.strip())

    @property
    def producao(self) -> bool:
        return self.ambiente is Ambiente.PROD

    @property
    def docs_habilitados(self) -> bool:
        """Swagger fica fora do ar em producao: expoe superficie de ataque."""
        return not self.producao


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Instancia unica de configuracao (injetada via Depends na API)."""
    return Settings()
