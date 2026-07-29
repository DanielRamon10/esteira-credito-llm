# Imagem do kyc-compliance.
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

WORKDIR /app

# Metadado antes do codigo: mudar uma linha do dominio nao reinstala dependencia.
COPY pyproject.toml README.md ./
COPY src/kyc_compliance/__init__.py src/kyc_compliance/__init__.py

RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /app/.venv && uv pip install .

COPY src/ src/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --no-deps --reinstall-package kyc-compliance .


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

COPY --from=construcao --chown=kyc:kyc /app/.venv /app/.venv
COPY --from=construcao --chown=kyc:kyc /app/src /app/src

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
COPY --chown=kyc:kyc dados/ dados/

USER kyc

EXPOSE 8100

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -fsS http://localhost:8100/health || exit 1

CMD ["python", "-m", "kyc_compliance"]
