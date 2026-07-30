# Imagem do customer-support.
#
# ## O contexto de build e a RAIZ do repositorio, nao o diretorio do servico
#
# Isso mudou quando `packages/plataforma` passou a existir. A dependencia de caminho
# (`plataforma = { path = "../../packages/plataforma" }`) aponta para fora do
# diretorio do servico, e o Docker nao consegue copiar nada que esteja acima do
# contexto — o build falhava com "path outside the build context".
#
# Duas alternativas foram descartadas: publicar a biblioteca num registry privado
# (infraestrutura nova para um monorepo de tres servicos) e copiar os arquivos para
# dentro de cada servico no build (que reintroduz a duplicacao que a extracao
# removeu, so mais tarde).
#
# O preco de usar a raiz e um contexto de build maior; o `.dockerignore` da raiz
# limita o que entra.
#
#     docker build -f services/customer-support/Dockerfile -t customer-support:dev .
#
# No compose isso e o `context: .` com `dockerfile:` apontando para ca.

#
# ## O terceiro servico, e o mais leve dos tres
#
# Este servico faz RAG — e mesmo assim nao carrega modelo de embedding.
#
# A medicao em `tests/eval/test_busca_qualidade.py` mostra BM25 sozinho acertando
# 92% no top-1 e 100% no top-3 sobre a base real de artigos. Com esse numero, o
# e5-large de 2,24GB seria peso sem ganho: a base tem dezenas de artigos curtos e
# autocontidos, e nao o corpus tecnico cheio de parafrase do `credit-analysis`.
#
# O efeito e uma imagem pequena e, mais importante, **sem cold start**: nao ha
# 5,8s na primeira consulta esperando um modelo baixar. Num canal de atendimento
# onde o cliente esta do outro lado, isso importa mais que no fluxo interno.
#
# A base de conhecimento entra na imagem por decisao de disponibilidade: sem ela o
# servico responderia toda duvida com "nao sei", e o cliente concluiria que a
# informacao nao existe.

# ---------------------------------------------------------------- construcao
FROM python:3.12-slim-trixie AS construcao

COPY --from=ghcr.io/astral-sh/uv:0.9.9 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /repo/services/customer-support

# Metadado antes do codigo: mudar uma linha do dominio nao reinstala dependencia.
# A biblioteca compartilhada, ANTES do `uv pip install`: sem ela no disco a
# dependencia de caminho nao resolve.
#
# O estagio de build **replica a estrutura do repositorio**: o servico em
# `/repo/services/<nome>` e a biblioteca em `/repo/packages/plataforma`. Assim o
# `path = "../../packages/plataforma"` do pyproject resolve sem override.
#
# A primeira tentativa foi copiar para `/packages/plataforma` mantendo o
# `WORKDIR /app`, e o `uv` recusou: `/app/../../packages` sobe acima da raiz, e ele
# nao normaliza caminho que escapa do diretorio base. A mensagem foi clara —
# "cannot normalize a relative path beyond the base directory" — e a licao e que
# dependencia de caminho num monorepo exige que a imagem espelhe o layout.
COPY packages/plataforma/ /repo/packages/plataforma/
COPY services/customer-support/pyproject.toml services/customer-support/README.md ./
COPY services/customer-support/src/customer_support/__init__.py src/customer_support/__init__.py

RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /opt/venv \
 && uv pip install --python /opt/venv/bin/python .

COPY services/customer-support/src/ src/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /opt/venv/bin/python --no-deps --reinstall-package customer-support .


# -------------------------------------------------------------------- runtime
FROM python:3.12-slim-trixie AS runtime

# `curl` e a unica coisa instalada, e so para o healthcheck. Poderia ser evitado
# com um script Python, mas `curl -fsS` no HEALTHCHECK e o padrao que qualquer
# pessoa de operacao le sem pensar.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 suporte \
 && useradd --uid 10001 --gid suporte --create-home --shell /usr/sbin/nologin suporte

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SUP_HOST=0.0.0.0 \
    SUP_PORTA=8200 \
    SUP_LOG_JSON=true

WORKDIR /app

# A biblioteca compartilhada tambem vai para o runtime, e o motivo e sutil.
#
# O `pyproject` declara a dependencia como **editavel** (`editable = true`), que e o
# que se quer em desenvolvimento: editar `plataforma` reflete nos tres servicos sem
# reinstalar. Numa imagem, porem, o venv guarda apenas um ponteiro para o caminho de
# origem — e copiar so o `/opt/venv` deixa esse ponteiro apontando para nada.
#
# O sintoma foi exatamente esse: `ModuleNotFoundError: No module named 'plataforma'`
# num container cujo `uv pip install` tinha passado sem erro.
#
# Copiar o pacote (cinco arquivos, ~30KB) e mais simples que trocar a instalacao por
# nao-editavel no build, o que exigiria contornar o `[tool.uv.sources]` do proprio
# pyproject e faria imagem e desenvolvimento divergirem.
COPY --from=construcao --chown=suporte:suporte /repo/packages/plataforma /repo/packages/plataforma

COPY --from=construcao --chown=suporte:suporte /opt/venv /app/.venv
COPY --from=construcao --chown=suporte:suporte /repo/services/customer-support/src /app/src

# As listas entram na imagem, e essa e uma diferenca deliberada em relacao ao
# corpus de politicas do outro servico (que fica fora, porque a ingestao e passo
# de deploy).
#
# Aqui a lista e carregada no boot e o servico **se recusa a subir sem ela** — uma
# instancia sem lista aprovaria todo mundo reportando "nenhuma correspondencia".
# Deixar o arquivo fora da imagem transformaria essa garantia em dependencia de
# volume montado corretamente, o que e uma forma de falhar em silencio no dia em
# que alguem esquecer o mount.
#
# Em producao a lista oficial vem de fonte externa e substitui estas por volume ou
# initContainer; o conteudo versionado aqui e sintetico (ver dados/listas/README.md).
COPY --chown=suporte:suporte services/customer-support/conhecimento/ conhecimento/

USER suporte

EXPOSE 8200

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -fsS http://localhost:8200/health || exit 1

CMD ["python", "-m", "customer_support"]
