# Imagem do credit-analysis.
#
# ## Multi-stage, e o que fica de fora
#
# O estagio `construcao` tem uv, compilador e cabecalhos; o estagio final tem
# apenas o venv pronto e o runtime. A diferenca nao e so tamanho: cada pacote de
# build presente na imagem final e superficie de ataque num container que roda em
# producao com acesso a banco e a documento de cliente.
#
# ## O que NAO entra na imagem, de proposito
#
# **O modelo de embedding (2,24GB).** Assar o e5-large aqui deixaria a imagem
# gigante e acoplaria a versao do modelo a versao da aplicacao — trocar de modelo
# viraria rebuild e redeploy. Ele e baixado no primeiro uso, para um cache que em
# Kubernetes e um volume. O custo dessa escolha esta medido: ~5,8s na primeira
# consulta de politica (ver `metricas.py`), contra ~150ms nas seguintes.
#
# **O corpus indexado.** A ingestao e passo de deploy, nao de build: o mesmo
# binario precisa servir ambientes com corpora diferentes. Os arquivos `.md` das
# politicas entram (sao a fonte), o indice no pgvector nao.
#
# ## Tesseract entra, e custa
#
# O binario mais o pacote de portugues adicionam ~90MB. A alternativa seria uma
# imagem separada so para OCR, o que resolveria o tamanho e criaria um salto de
# rede no meio do processamento de documento. Para um servico que le holerite
# como parte do fluxo principal, o binario local ganha.
#
# ## Tamanho, medido e sem desculpa: 1,18GB
#
# Nao e pouco, e o motivo esta nas dependencias, nao no Dockerfile. Os cinco
# maiores pacotes do venv (614MB no total) sao:
#
#     opencv (headless) + libs   153MB    pre-processamento de imagem
#     pandas                      73MB    analise de extrato bancario
#     pymupdf                     64MB    camada de texto de PDF
#     onnxruntime                 58MB    inferencia do modelo de embedding
#     numpy + libs                70MB    motor de score
#
# Ou seja: **o peso e o preco de fazer OCR, RAG e analise de extrato no mesmo
# processo.** Cortar exigiria separar servicos — e a Camada 6 nao e o lugar de
# fazer isso, porque a fronteira certa entre eles vem do dominio e nao do
# tamanho da imagem.
#
# O que ja esta feito para nao piorar: `opencv-python-headless` em vez do pacote
# completo (que arrastaria GTK e X11), `--no-install-recommends` no apt, o modelo
# de embedding fora da imagem e o venv copiado de outro estagio.

# ---------------------------------------------------------------- construcao
FROM python:3.12-slim-trixie AS construcao

# uv em vez de pip: resolve e instala em segundos, e o lockfile de dependencia
# fica reprodutivel. A imagem oficial do uv da o binario sem instalar Rust.
COPY --from=ghcr.io/astral-sh/uv:0.9.9 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Copia so o metadado antes do codigo. E o que faz a camada de dependencias ser
# reaproveitada: mudar uma linha de `scoring.py` nao reinstala nada.
COPY pyproject.toml README.md ./
COPY src/credit_analysis/__init__.py src/credit_analysis/__init__.py

RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /app/.venv && uv pip install .

COPY src/ src/
COPY politicas/ politicas/

# Reinstala apenas o proprio pacote depois do codigo. A instalacao acima ja
# trouxe as dependencias (a partir do metadado, com um `__init__.py` vazio como
# fonte); esta segunda passagem so troca o pacote pelo codigo completo, deixando
# a camada pesada de dependencias intacta entre builds.
#
# `uv pip install` nao aceita `--no-editable`: instalacao sem `-e` ja e o padrao.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --no-deps --reinstall-package credit-analysis .


# -------------------------------------------------------------------- runtime
FROM python:3.12-slim-trixie AS runtime

# Tesseract com portugues. `--no-install-recommends` evita arrastar dezenas de
# pacotes de documentacao e fonte que nao servem a um container.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tesseract-ocr \
      tesseract-ocr-por \
      curl \
 && rm -rf /var/lib/apt/lists/*

# Usuario sem privilegio. Container que roda como root e um `docker run
# --privileged` acidental de distancia de comprometer o host, e nenhuma parte
# deste servico precisa de root.
RUN groupadd --gid 10001 credito \
 && useradd --uid 10001 --gid credito --create-home --shell /usr/sbin/nologin credito

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Cache do modelo de embedding. Em Kubernetes isto e um volume; sem volume, o
    # download de 2,24GB se repete a cada restart do pod.
    HF_HOME=/cache/huggingface \
    FASTEMBED_CACHE_PATH=/cache/fastembed \
    CREDIT_HOST=0.0.0.0 \
    CREDIT_PORTA=8000 \
    CREDIT_LOG_JSON=true

WORKDIR /app

COPY --from=construcao --chown=credito:credito /app/.venv /app/.venv
COPY --from=construcao --chown=credito:credito /app/src /app/src
COPY --from=construcao --chown=credito:credito /app/politicas /app/politicas

RUN mkdir -p /cache && chown -R credito:credito /cache

USER credito

EXPOSE 8000

# `/health` e nao `/ready`: healthcheck de container responde "o processo esta
# vivo?", e usar o readiness aqui faria o Docker matar um container saudavel cuja
# dependencia esta lenta. Quem decide sobre trafego e o orquestrador, via /ready.
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# `python -m credit_analysis` e nao `uvicorn`: o entrypoint constroi o servidor
# com a event loop correta. Em Linux daria no mesmo, mas manter um unico comando
# entre desenvolvimento e producao elimina uma classe de "funciona na minha
# maquina" — ver `infrastructure/event_loop.py`.
CMD ["python", "-m", "credit_analysis"]
