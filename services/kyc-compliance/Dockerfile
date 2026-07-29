# Imagem do kyc-compliance.
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
#     docker build -f services/kyc-compliance/Dockerfile -t kyc-compliance:dev .
#
# No compose isso e o `context: .` com `dockerfile:` apontando para ca.

#
# ## O contraste com o outro servico e o argumento a favor de separar
#
# A imagem do `credit-analysis` tem 1,18GB: OpenCV, pandas, PyMuPDF, ONNX Runtime e
# NumPy somam 614MB de dependencia nativa, porque aquele servico faz OCR, RAG e
# analise de extrato no mesmo processo.
#
# Este nao faz nada disso. O dominio e comparacao de string, resolvida com
# `difflib` e `unicodedata` da biblioteca padrao — sete dependencias contra vinte e
# uma, e nenhuma com codigo nativo pesado. A imagem sai em torno de 200MB.
#
# A diferenca importa para mais do que disco. Este servico e **dependencia** do
# outro: quando ele cai, toda analise de credito vai para revisao humana. Um
# servico nessa posicao precisa subir rapido, escalar barato e ter pouca superficie
# de CVE — e e exatamente o que se ganha por nao carregar 600MB que ele nunca usa.
#
# Sem Tesseract, sem modelo de embedding, sem cliente de banco: nao ha o que
# baixar no primeiro uso e nao ha cold start.

# ---------------------------------------------------------------- construcao
FROM python:3.12-slim-trixie AS construcao

COPY --from=ghcr.io/astral-sh/uv:0.9.9 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /repo/services/kyc-compliance

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
COPY services/kyc-compliance/pyproject.toml services/kyc-compliance/README.md ./
COPY services/kyc-compliance/src/kyc_compliance/__init__.py src/kyc_compliance/__init__.py

RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /opt/venv \
 && uv pip install --python /opt/venv/bin/python .

COPY services/kyc-compliance/src/ src/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /opt/venv/bin/python --no-deps --reinstall-package kyc-compliance .


# -------------------------------------------------------------------- runtime
FROM python:3.12-slim-trixie AS runtime

# `curl` e a unica coisa instalada, e so para o healthcheck. Poderia ser evitado
# com um script Python, mas `curl -fsS` no HEALTHCHECK e o padrao que qualquer
# pessoa de operacao le sem pensar.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 kyc \
 && useradd --uid 10001 --gid kyc --create-home --shell /usr/sbin/nologin kyc

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    KYC_HOST=0.0.0.0 \
    KYC_PORTA=8100 \
    KYC_LOG_JSON=true

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
COPY --from=construcao --chown=kyc:kyc /repo/packages/plataforma /repo/packages/plataforma

COPY --from=construcao --chown=kyc:kyc /opt/venv /app/.venv
COPY --from=construcao --chown=kyc:kyc /repo/services/kyc-compliance/src /app/src

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
COPY --chown=kyc:kyc services/kyc-compliance/dados/ dados/

USER kyc

EXPOSE 8100

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -fsS http://localhost:8100/health || exit 1

CMD ["python", "-m", "kyc_compliance"]
