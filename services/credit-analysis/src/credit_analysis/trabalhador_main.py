"""Trabalhador de extracao como processo separado.

    python -m credit_analysis.trabalhador_main

## O que a Camada 9 destravou

Este arquivo passou a Camada 8 inteira **recusando subir**, e o motivo era real: nao existia
repositorio de analise compartilhado. Cada processo veria o proprio dicionario, a API anexaria o
documento no dela, e o trabalhador consumiria a mensagem sem achar o documento — erro permanente,
portanto todo documento iria para `falhou`.

Com `RepositorioAnalisesPostgres` isso deixou de valer. O trabalhador le a analise que a API gravou,
e o bloqueio otimista cuida da corrida entre os dois.

## Por que um processo separado, agora que e possivel

1. **Escala independente.** A API atende requisicoes de milissegundos; a extracao leva segundos e
   pode chamar modelo de visao. Juntos, dimensionar um significa dimensionar o outro — o HPA
   escalaria a API inteira porque a fila cresceu.
2. **Falha isolada.** Um OCR que estoura memoria mata o processo. Sendo o mesmo da API, leva as
   requisicoes em voo junto.
3. **Mais de uma replica de API.** Com o trabalhador em processo, so uma replica podia consumir —
   a que publica pode nao ser a que consome. Separado, a API escala livremente.

## Por que ele nao tem sonda, mas **tem** `/metrics`

A primeira versao deste arquivo dizia "sem servidor HTTP" e tratava as duas coisas como uma. Elas
nao sao, e a diferenca foi medida contra o Prometheus do compose:

- **sonda** afirma saude. Para consumidor de fila ela mede a coisa errada: o processo estar vivo
  nao diz que ele esta consumindo, e um 200 num trabalhador travado e pior que nada. Por isso o
  Deployment nao tem `livenessProbe` nem `readinessProbe`, e isso continua deliberado;
- **`/metrics`** nao afirma nada; ele expoe contador. E `credito_extracoes_total` e incrementado
  **aqui**, neste processo — em nenhum outro.

Sem o endpoint, o efeito foi este: tres documentos processados de ponta a ponta, e a consulta
`credito_extracoes_total` no Prometheus devolvendo vetor vazio. Com a serie ausente, a expressao do
alerta `DocumentosPresosNaExtracao` (`recebidos - extracoes > 5`) vira operacao entre vetor e vetor
vazio, cujo resultado em PromQL e **vazio** — o alerta que existe para detectar trabalhador parado
nao dispararia nunca, e ficou assim justamente ao separar o trabalhador.

O sinal de fila continua sendo o de fora (`ApproximateAgeOfOldestMessage` no CloudWatch); o que
este endpoint recupera e o lado de dentro.
"""

from __future__ import annotations

import structlog
from plataforma.logging import configurar_logging
from prometheus_client import start_http_server

from credit_analysis.application.use_cases.extracao_assincrona import ExtrairDocumento
from credit_analysis.application.use_cases.processar_documento import AplicarExtracao
from credit_analysis.application.use_cases.trabalhador import Trabalhador, laco
from credit_analysis.config import Settings, get_settings
from credit_analysis.infrastructure.armazenamento.s3 import ArmazenamentoS3
from credit_analysis.infrastructure.armazenamento.sqs import FilaSQS
from credit_analysis.infrastructure.bureau import BureauStub
from credit_analysis.infrastructure.event_loop import executar
from credit_analysis.infrastructure.observabilidade.metricas import REGISTRO
from credit_analysis.infrastructure.rag.pgvector_store import criar_pool
from credit_analysis.infrastructure.repositories.postgres import RepositorioAnalisesPostgres

logger = structlog.get_logger(__name__)

# Porta do endpoint de metricas.
#
# **8001 e nao 8000**, apesar de nada mais neste container ocupar a 8000. A razao e a mesma que fez
# o compose publicar a API em 8080: alvo do Prometheus com porta ambigua ja custou um painel que
# contava em dobro. Um numero diferente torna impossivel confundir um alvo com o outro ao ler
# `prometheus.yml`, e deixa claro para quem escreve um Service que aqui nao ha API.
#
# Constante e nao variavel de ambiente: uma porta configuravel exigiria manter o valor sincronizado
# entre `Settings`, o compose, o manifest e o scrape — quatro lugares para uma escolha que ninguem
# precisa mudar.
PORTA_DE_METRICAS = 8001


def _conferir_dependencias(settings: Settings) -> None:
    """Recusa subir sem o que o trabalhador precisa, com o motivo.

    As tres verificacoes existem porque as tres falhas sao **silenciosas** de formas diferentes:

    - sem S3 e fila, ele consumiria uma fila em memoria do proprio processo, vazia por construcao
      (quem publica e a API, em outro processo). Ficaria rodando sem processar nada, sem erro;
    - sem Postgres, ele nao veria o documento que a API anexou e mandaria **todo** documento para
      `falhou` — o que parece problema de OCR e nao de configuracao;
    - sem OCR, a extracao falharia em toda mensagem.

    Nenhuma delas produz um sintoma que aponte para a causa, e e por isso que a checagem e no boot.
    """
    if not settings.usar_armazenamento_real:
        raise RuntimeError(
            "o trabalhador exige CREDIT_BUCKET_DOCUMENTOS e CREDIT_FILA_EXTRACAO_URL. Com "
            "adapters em memoria ele consumiria uma fila vazia do proprio processo, e a extracao "
            "nunca aconteceria — sem erro que apontasse a causa."
        )

    if not settings.usar_pgvector:
        raise RuntimeError(
            "o trabalhador exige CREDIT_POSTGRES_DSN: com repositorio em memoria, cada processo "
            "tem o seu, e ele nao veria o documento que a API anexou — toda extracao falharia com "
            "'documento nao esta na analise', o que parece problema de OCR e nao de configuracao."
        )


def main() -> None:
    settings = get_settings()
    configurar_logging(nivel=settings.nivel_log, formato_json=settings.log_json)
    _conferir_dependencias(settings)

    # Importado aqui e nao no topo: `_montar_ocr` arrasta OpenCV, PyMuPDF e ONNX, e este modulo e
    # importado por analise estatica e autocompletar de IDE, que nao deveriam pagar por isso.
    from credit_analysis.api.app import _montar_ocr

    motor = _montar_ocr(settings)
    if motor is None:
        # A terceira dependencia silenciosa: sem motor, a extracao falharia em toda mensagem, e o
        # log diria "falha na extracao" sobre um servico que nunca teve como extrair.
        #
        # Levanta aqui e nao no `_conferir_dependencias` porque montar o motor **e** a verificacao:
        # ele depende do Tesseract no PATH, e nao de variavel de ambiente.
        raise RuntimeError(
            "o trabalhador exige um motor de OCR disponivel. Sem Tesseract (ou credencial de "
            "visao), toda extracao falharia — instale o Tesseract com o pacote `por`."
        )

    pool = criar_pool(settings.postgres_dsn, minimo=1, maximo=4)
    repositorio = RepositorioAnalisesPostgres(pool)

    trabalhador = Trabalhador(
        fila=FilaSQS(
            url_da_fila=settings.fila_extracao_url,
            regiao=settings.regiao_aws,
            endpoint_url=settings.sqs_endpoint or None,
        ),
        extrair=ExtrairDocumento(
            armazenamento=ArmazenamentoS3(
                bucket=settings.bucket_documentos,
                regiao=settings.regiao_aws,
                endpoint_url=settings.s3_endpoint or None,
            ),
            motor_ocr=motor,
        ),
        # `BureauStub` e nao o cliente real: o bureau entra na reavaliacao, que acontece dentro de
        # `AplicarExtracao`. Com um bureau que consulta rede de verdade, cada extracao pagaria a
        # latencia dele — e o resultado ja esta no parecer que a API produziu.
        #
        # Nao e simplificacao: e a mesma escolha do `criar_app` quando `bureau` nao e injetado.
        aplicar=AplicarExtracao(repositorio=repositorio, bureau=BureauStub()),
        repositorio=repositorio,
    )

    # Endpoint de metricas antes do laco, e com o mesmo `REGISTRO` que a aplicacao usa.
    #
    # `start_http_server` sobe uma thread propria com um servidor minimo. Ele nao serve rota
    # nenhuma alem de `/metrics` — em particular nao serve `/health`, e a ausencia e o ponto: ver o
    # cabecalho deste arquivo sobre a diferenca entre expor contador e afirmar saude.
    #
    # Registry explicito e nao o default: as metricas deste servico vivem em `REGISTRO`, e o
    # `start_http_server` sem argumento serviria o `REGISTRY` global — que aqui esta vazio. O
    # sintoma seria um alvo `up == 1` respondendo 200 com corpo praticamente vazio, ou seja
    # exatamente a aparencia de funcionamento que o endpoint deveria dissolver.
    start_http_server(PORTA_DE_METRICAS, registry=REGISTRO)

    logger.info(
        "trabalhador.iniciado",
        bucket=settings.bucket_documentos,
        fila=settings.fila_extracao_url,
        repositorio="postgres",
        porta_de_metricas=PORTA_DE_METRICAS,
    )

    async def rodar() -> None:
        # O pool abre aqui, dentro do loop: abrir no construtor dispara I/O fora do event loop e o
        # psycopg emite aviso. Mesmo motivo do `open=False` em `criar_pool`.
        await pool.open(wait=True, timeout=30)
        try:
            await laco(trabalhador)
        finally:
            await pool.close()

    # `executar` e nao `asyncio.run`: no Windows o default e o ProactorEventLoop, sobre o qual o
    # psycopg async nao roda — o mesmo ajuste que o `__main__` da API faz.
    executar(rodar())


if __name__ == "__main__":
    main()
