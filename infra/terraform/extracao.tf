# Fila de extracao, DLQ e a Lambda de OCR.
#
# ## Estado honesto deste arquivo
#
# **Nunca foi aplicado.** Passa em `terraform validate` e nao existe conta AWS neste projeto. O que
# roda de verdade e o equivalente local — MinIO e ElasticMQ no compose, com o trabalhador dentro do
# processo da API — e o handler da extracao e **o mesmo codigo** nos dois caminhos.
#
# O que fica sem exercicio, e vale nomear: o runtime da Lambda, o empacotamento em container image,
# e o gatilho da fila. O que roda em ambos: `ExtrairDocumento`, os adapters de S3 e SQS, e a
# classificacao de erro.

# ------------------------------------------------------------------- Filas

resource "aws_sqs_queue" "extracao_dlq" {
  name = "${local.prefixo}-extracao-dlq"

  # 14 dias, o maximo do SQS.
  #
  # Mensagem em DLQ e documento que **nao** foi processado, e alguem precisa olhar. Com a retencao
  # padrao de 4 dias, um lote que estoure numa sexta-feira desaparece antes de a semana comecar —
  # e o cliente ficaria com o documento em `falhou` sem ninguem saber por que.
  message_retention_seconds = 1209600

  sqs_managed_sse_enabled = true
}

resource "aws_sqs_queue" "extracao" {
  name = "${local.prefixo}-extracao"

  # 300s, o mesmo valor de `VISIBILIDADE_SEGUNDOS` no adapter e no `elasticmq.conf`.
  #
  # Precisa cobrir o pior caso do OCR — 148s medidos neste projeto com escalonamento para modelo de
  # visao. Menor que isso, o SQS reentrega **enquanto o trabalhador ainda processa**: a
  # idempotencia salva o resultado, e o custo de OCR e pago duas vezes.
  #
  # Os tres lugares com o mesmo numero sao uma duplicacao real. Ela existe porque cada um e lido
  # por uma ferramenta diferente (Python, HOCON, HCL) e nao ha fonte comum; o que os mantem juntos
  # e o comentario em cada um apontando para os outros.
  visibility_timeout_seconds = 300

  # Long polling no lado da fila tambem. O adapter ja pede `WaitTimeSeconds`, mas um consumidor
  # futuro que esqueca herda o comportamento certo — e o SQS cobra por requisicao.
  receive_wait_time_seconds = 20

  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.extracao_dlq.arn
    # **O teto de tentativas mora aqui**, e nao no codigo.
    #
    # E a diferenca central entre o adapter em memoria e o de SQS: em memoria a contagem e nossa,
    # aqui e da fila. Duas contagens divergiriam, e a da fila sobrevive ao reinicio do trabalhador
    # enquanto a nossa nao — um documento com quatro de cinco tentativas voltaria a ter cinco
    # depois de um deploy.
    #
    # Consequencia pratica: mudar de 3 para 5 e um `apply`, nao um deploy.
    maxReceiveCount = 3
  })
}

# Alarme de mensagem na DLQ.
#
# Sem ele, a DLQ e um lugar onde documento vai morrer em silencio. O sinal nao e "quantas" e sim
# "existe alguma": uma unica mensagem ali significa um cliente cujo documento nao foi processado.
resource "aws_cloudwatch_metric_alarm" "dlq_com_mensagem" {
  alarm_name        = "${local.prefixo}-extracao-dlq-com-mensagem"
  alarm_description = "Documento estourou as tentativas de extracao e foi para a DLQ."

  namespace   = "AWS/SQS"
  metric_name = "ApproximateNumberOfMessagesVisible"
  dimensions  = { QueueName = aws_sqs_queue.extracao_dlq.name }

  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  # `notBreaching`: sem mensagem, o SQS nao publica a metrica, e o alarme ficaria em
  # `INSUFFICIENT_DATA` para sempre — indistinguivel de alarme quebrado.
  treat_missing_data = "notBreaching"
}

# Alarme de fila crescendo.
#
# E o sinal que substitui a sonda de liveness do trabalhador. Processo vivo nao significa
# consumindo, e um HTTP 200 num trabalhador travado seria pior que nada; profundidade de fila
# subindo detecta os dois casos — trabalhador morto e trabalhador lento.
resource "aws_cloudwatch_metric_alarm" "fila_acumulando" {
  alarm_name        = "${local.prefixo}-extracao-acumulando"
  alarm_description = "Fila de extracao acumulando: trabalhador parado, lento, ou pico real."

  namespace   = "AWS/SQS"
  metric_name = "ApproximateAgeOfOldestMessage"
  dimensions  = { QueueName = aws_sqs_queue.extracao.name }

  statistic = "Maximum"
  period    = 300
  # Tres periodos: um pico de 5 minutos e trabalho normal com OCR de 148s. Quinze minutos com a
  # mensagem mais antiga parada nao e.
  evaluation_periods  = 3
  threshold           = 900
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
}


# ------------------------------------------------------------------ Lambda

resource "aws_ecr_repository" "extracao" {
  # Repositorio proprio, separado do da API.
  #
  # A imagem da Lambda carrega Tesseract e OpenCV e **nao** carrega FastAPI, LangGraph nem o
  # cliente de Postgres — ela e a metade que nao conhece o dominio de credito. Compartilhar o
  # repositorio da API significaria implantar na Lambda uma imagem de 1,18GB para usar um terco
  # dela, e o cold start paga por cada MB.
  name                 = "${var.nome_do_projeto}-extracao"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

data "aws_iam_policy_document" "assumir_lambda" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "extracao" {
  name               = "${local.prefixo}-extracao"
  assume_role_policy = data.aws_iam_policy_document.assumir_lambda.json
}

data "aws_iam_policy_document" "extracao" {
  # Ler o documento. **Somente leitura**, e nao `PutObject`.
  #
  # A extracao le e produz texto; ela nao tem motivo para escrever no bucket. Com permissao de
  # escrita, um comprometimento da funcao — que processa arquivo enviado por terceiro, ou seja a
  # superficie mais exposta do sistema — poderia sobrescrever o documento que sustenta um parecer.
  statement {
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = ["${aws_s3_bucket.documentos.arn}/*"]
  }

  # Consumir da fila. Sem `SendMessage`: a funcao processa, nao enfileira.
  statement {
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
      "sqs:ChangeMessageVisibility",
    ]
    resources = [aws_sqs_queue.extracao.arn]
  }
}

resource "aws_iam_role_policy" "extracao" {
  name   = "extracao"
  role   = aws_iam_role.extracao.id
  policy = data.aws_iam_policy_document.extracao.json
}

resource "aws_iam_role_policy_attachment" "extracao_logs" {
  role       = aws_iam_role.extracao.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_cloudwatch_log_group" "extracao" {
  name              = "/aws/lambda/${local.prefixo}-extracao"
  retention_in_days = var.dias_retencao_log
}

resource "aws_lambda_function" "extracao" {
  function_name = "${local.prefixo}-extracao"
  role          = aws_iam_role.extracao.arn

  # **Container image**, e nao zip com layer.
  #
  # O Tesseract com o pacote de portugues, mais OpenCV e PyMuPDF, passa do limite de 250MB
  # descompactado do zip. Layer resolveria o Tesseract e ainda deixaria as bibliotecas nativas
  # apertadas — e exigiria manter um processo de build de layer separado do Dockerfile que ja
  # existe.
  #
  # Container aceita ate 10GB e reaproveita o mesmo Dockerfile multi-stage. O preco e cold start
  # maior, e ele importa menos aqui: a extracao ja e assincrona, e o cliente nao espera.
  package_type = "Image"
  image_uri    = "${aws_ecr_repository.extracao.repository_url}:${var.tag_imagem}"

  # 3GB. O OCR carrega a imagem rasterizada inteira em memoria, e um extrato de 20 paginas a 200
  # DPI passa de 1GB so de pixels. Na Lambda a CPU e proporcional a memoria, entao 3GB tambem
  # compra o processamento — em 512MB o mesmo documento levaria minutos.
  memory_size = 3008

  # 300s, casado com o `visibility_timeout_seconds` da fila.
  #
  # Os dois **precisam** bater: com timeout da funcao maior que a visibilidade, o SQS reentrega
  # enquanto a primeira invocacao ainda roda, e o documento e processado duas vezes. Com menor, a
  # funcao morre no meio e a mensagem volta — recuperavel, mas paga OCR jogado fora.
  timeout = 300

  environment {
    variables = {
      CREDIT_BUCKET_DOCUMENTOS = aws_s3_bucket.documentos.id
      CREDIT_AMBIENTE          = var.ambiente
      CREDIT_LOG_JSON          = "true"
    }
  }

  logging_config {
    log_format = "JSON"
    log_group  = aws_cloudwatch_log_group.extracao.name
  }

  depends_on = [aws_iam_role_policy_attachment.extracao_logs]
}

resource "aws_lambda_event_source_mapping" "extracao" {
  event_source_arn = aws_sqs_queue.extracao.arn
  function_name    = aws_lambda_function.extracao.arn

  # Uma mensagem por invocacao.
  #
  # Lote maior seria mais barato em invocacoes e pior no que importa: com 10 mensagens e uma
  # falhando, o comportamento padrao devolve **o lote inteiro** para a fila, e as nove que deram
  # certo sao reprocessadas — nove OCRs pagos de novo.
  #
  # `ReportBatchItemFailures` resolveria isso e exige o handler devolver a lista de falhas, o que
  # acopla o codigo ao formato da Lambda. Com lote 1, o problema nao existe.
  batch_size = 1

  # Duas invocacoes simultaneas no maximo.
  #
  # Nao e para economizar: e porque a extracao termina publicando o resultado, e a API do outro
  # lado tem capacidade finita. Sem teto, um acumulo de 500 mensagens dispararia 500 invocacoes de
  # 3GB e o pico chegaria inteiro na API.
  scaling_config {
    maximum_concurrency = 2
  }
}
