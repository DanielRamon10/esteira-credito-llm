# Infraestrutura do credit-analysis na AWS.
#
# ## Fargate e nao EC2, e por que existem tambem manifests de Kubernetes
#
# Este arquivo provisiona ECS Fargate. Os manifests em `infra/k8s/` sao o
# **caminho alternativo**, para quem ja tem cluster — e nao um segundo ambiente.
# A duplicacao e proposital e vale explicar em vez de esconder: a escolha entre
# os dois nao e tecnica em abstrato, e sim "ja existe cluster e time de
# plataforma?". Sem cluster, Fargate elimina gestao de no; com cluster, subir um
# runtime separado so para um servico e desperdicio.
#
# EC2 ficaria de fora nos dois casos: patch de sistema operacional, autoscaling
# group e AMI dourada sao trabalho que nao agrega nada a uma API sem estado.
#
# ## Rede: VPC default por escolha de escopo
#
# Uma VPC propria (subnets publicas e privadas em tres AZs, NAT, tabelas de rota)
# sao ~150 linhas que nao ensinam nada sobre este servico. O `data source` da
# default deixa o codigo focado no que e especifico. **Em producao de verdade isto
# nao serve**: as tasks precisam ficar em subnet privada com NAT, e o comentario
# fica aqui para que a limitacao seja lida, e nao descoberta.

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

data "aws_caller_identity" "atual" {}

locals {
  prefixo = "${var.nome}-${var.ambiente}"
}


# ---------------------------------------------------------------------- ECR

resource "aws_ecr_repository" "api" {
  name = var.nome

  # `IMMUTABLE`: impede sobrescrever uma tag ja publicada. Sem isso, `v1.2.3`
  # pode passar a apontar para outro conteudo, e o rollback deixa de ser
  # confiavel — o que e exatamente quando se precisa dele.
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    # Scan a cada push. A imagem tem ~1,18GB de dependencia nativa (OpenCV,
    # PyMuPDF, ONNX Runtime), que e onde CVE aparece.
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "api" {
  repository = aws_ecr_repository.api.name

  # Sem politica de ciclo de vida, um repositorio com imagem de 1,18GB e deploy
  # diario custa mais de armazenamento que a computacao do servico em um ano.
  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Mantem as 10 ultimas imagens de release"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["v"]
          countType     = "imageCountMoreThan"
          countNumber   = 10
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Expira imagem sem tag em 7 dias (camada orfa de build)"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 7
        }
        action = { type = "expire" }
      },
    ]
  })
}


# ------------------------------------------------------------- Armazenamento

resource "aws_s3_bucket" "documentos" {
  # Nome com account id porque nome de bucket e global: sem sufixo, um `apply` em
  # outra conta colide com um bucket que nao e seu.
  bucket = "${local.prefixo}-documentos-${data.aws_caller_identity.atual.account_id}"
}

resource "aws_s3_bucket_public_access_block" "documentos" {
  # Primeiro recurso a escrever, e nao um detalhe: este bucket guarda holerite e
  # extrato bancario. Vazamento de bucket de documento e o incidente mais comum e
  # mais caro que existe em nuvem.
  bucket                  = aws_s3_bucket.documentos.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "documentos" {
  bucket = aws_s3_bucket.documentos.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_versioning" "documentos" {
  bucket = aws_s3_bucket.documentos.id

  versioning_configuration {
    # Versionamento protege contra sobrescrita e delete acidental. Num bucket que
    # sustenta decisao de credito, "o documento que embasou este parecer" precisa
    # continuar recuperavel mesmo depois de um upload errado.
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "documentos" {
  bucket = aws_s3_bucket.documentos.id

  rule {
    id     = "expirar-documento-processado"
    status = "Enabled"

    filter {}

    # LGPD art. 15: dado pessoal nao deve ser guardado alem da finalidade. O
    # documento serve para apurar renda; passada a operacao, o que precisa
    # sobreviver e o parecer e a trilha, nao a imagem do holerite.
    expiration {
      days = 365
    }

    noncurrent_version_expiration {
      noncurrent_days = 90
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}


# ------------------------------------------------------------------- Segredos

resource "aws_secretsmanager_secret" "aplicacao" {
  name        = "${local.prefixo}/aplicacao"
  description = "DSN do Postgres e chave da Anthropic do credit-analysis."

  # Zero dias: permite recriar o segredo com o mesmo nome sem esperar a janela de
  # recuperacao. Em producao com dado critico o valor correto seria 7 a 30 dias —
  # aqui o trade-off pende para iteracao.
  recovery_window_in_days = 0
}

# Nao ha `aws_secretsmanager_secret_version` neste arquivo, e a ausencia e
# deliberada. Escrever o valor aqui o colocaria no **estado** do Terraform em
# texto claro, e o estado e um arquivo que vai para S3 e e lido por quem tem
# acesso ao bucket. O valor entra fora do Terraform (console, CLI ou rotacao
# automatica); daqui sai apenas o ARN, que o IAM abaixo autoriza a ler.


# ---------------------------------------------------------------------- IAM

# Duas roles, e a distincao e o ponto central deste bloco.
#
# `execucao` e usada pelo **agente do ECS** antes de o container subir: puxar
# imagem do ECR, escrever no CloudWatch, resolver segredo. `task` e usada pelo
# **codigo da aplicacao** em execucao: ler e gravar documento no S3.
#
# Juntar as duas daria ao codigo da aplicacao permissao de puxar imagem e ler
# qualquer segredo do servico — que e exatamente o que um comprometimento de
# aplicacao procura.

data "aws_iam_policy_document" "assumir_ecs" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execucao" {
  name               = "${local.prefixo}-execucao"
  assume_role_policy = data.aws_iam_policy_document.assumir_ecs.json
}

resource "aws_iam_role_policy_attachment" "execucao_padrao" {
  role       = aws_iam_role.execucao.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "ler_segredo" {
  statement {
    actions = ["secretsmanager:GetSecretValue"]
    # ARN especifico, nao `*`. Uma role que le qualquer segredo da conta e uma
    # escalada de privilegio esperando o momento.
    resources = [aws_secretsmanager_secret.aplicacao.arn]
  }
}

resource "aws_iam_role_policy" "execucao_segredo" {
  name   = "ler-segredo"
  role   = aws_iam_role.execucao.id
  policy = data.aws_iam_policy_document.ler_segredo.json
}

resource "aws_iam_role" "task" {
  name               = "${local.prefixo}-task"
  assume_role_policy = data.aws_iam_policy_document.assumir_ecs.json
}

data "aws_iam_policy_document" "documentos" {
  statement {
    actions = ["s3:GetObject", "s3:PutObject"]
    # Objetos dentro do bucket, nao o bucket. Sem o `/*`, a permissao nao
    # funciona; com o ARN do bucket incluido, `s3:PutObject` viraria permissao
    # sobre o proprio bucket.
    resources = ["${aws_s3_bucket.documentos.arn}/*"]
  }

  statement {
    # `ListBucket` separado porque age no bucket e nao no objeto. Sem o prefixo,
    # a aplicacao poderia listar o bucket inteiro.
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.documentos.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["documentos/*"]
    }
  }
}

resource "aws_iam_role_policy" "task_documentos" {
  name   = "documentos"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.documentos.json
}

# Sem `s3:DeleteObject` na role da aplicacao, de proposito: a expiracao e feita
# pela regra de ciclo de vida do bucket, que e auditavel e nao depende de codigo.
# Aplicacao que pode apagar documento pode apagar a evidencia de um parecer.


# ------------------------------------------------------------------ Execucao

resource "aws_cloudwatch_log_group" "api" {
  name              = "/ecs/${local.prefixo}"
  retention_in_days = var.dias_retencao_log
}

resource "aws_ecs_cluster" "principal" {
  name = local.prefixo

  setting {
    # Container Insights custa e paga por si no primeiro incidente: sem ele, nao
    # ha metrica de CPU e memoria por task, so agregado de cluster.
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_security_group" "api" {
  name        = "${local.prefixo}-api"
  description = "Trafego da API credit-analysis"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "HTTP da VPC"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    # CIDR da VPC e nao 0.0.0.0/0: quem expoe para a internet e o load balancer,
    # que tem o proprio security group. Task acessivel direto da internet
    # contorna WAF, log de acesso e terminacao TLS.
    cidr_blocks = [data.aws_vpc.default.cidr_block]
  }

  egress {
    description = "Saida para ECR, S3, Secrets Manager e Postgres"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    # Egress aberto e uma concessao consciente: restringir exigiria VPC endpoints
    # para ECR, S3, Secrets Manager e CloudWatch. Fica registrado como divida —
    # ver a NetworkPolicy em infra/k8s, onde o egress ja e fechado por namespace.
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_ecs_task_definition" "api" {
  family                   = local.prefixo
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.cpu_task
  memory                   = var.memoria_task
  execution_role_arn       = aws_iam_role.execucao.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([
    {
      name      = "api"
      image     = "${aws_ecr_repository.api.repository_url}:${var.tag_imagem}"
      essential = true

      portMappings = [
        {
          containerPort = 8000
          protocol      = "tcp"
        }
      ]

      environment = [
        { name = "CREDIT_AMBIENTE", value = var.ambiente },
        { name = "CREDIT_LOG_JSON", value = "true" },
        { name = "CREDIT_DOCS_HABILITADOS", value = "false" },
        # Explicito e nao `auto`: sem provedor disponivel o servico falha ao
        # subir, em vez de responder parecer com texto de um fake.
        { name = "CREDIT_PROVEDOR_LLM", value = "ollama" },
        { name = "CREDIT_MODELO_OLLAMA", value = "llama3.1:8b" },
        { name = "CREDIT_MODELO_AGENTE", value = "qwen2.5:7b" },
        { name = "CREDIT_BUCKET_DOCUMENTOS", value = aws_s3_bucket.documentos.id },
      ]

      # Segredo por referencia, nao por valor. O ECS resolve no momento de subir
      # o container, entao o valor nunca aparece na task definition — que e
      # legivel por qualquer um com `ecs:DescribeTaskDefinition`.
      secrets = [
        {
          name      = "CREDIT_POSTGRES_DSN"
          valueFrom = "${aws_secretsmanager_secret.aplicacao.arn}:CREDIT_POSTGRES_DSN::"
        },
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.api.name
          "awslogs-region"        = var.regiao
          "awslogs-stream-prefix" = "api"
        }
      }

      healthCheck = {
        # `/health` e nao `/ready`, pelo mesmo motivo do Dockerfile: quem decide
        # sobre trafego e o load balancer; este check decide se o container esta
        # vivo.
        command     = ["CMD-SHELL", "curl -fsS http://localhost:8000/health || exit 1"]
        interval    = 15
        timeout     = 3
        retries     = 3
        startPeriod = 60
      }
    }
  ])
}

resource "aws_ecs_service" "api" {
  name            = local.prefixo
  cluster         = aws_ecs_cluster.principal.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.replicas_desejadas
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = data.aws_subnets.default.ids
    security_groups = [aws_security_group.api.id]
    # `true` porque a task esta em subnet publica da VPC default e precisa
    # alcancar ECR e Secrets Manager. Em subnet privada com NAT isto vira
    # `false` — e essa e a configuracao correta para producao.
    assign_public_ip = true
  }

  deployment_circuit_breaker {
    # Sem circuit breaker, um deploy de imagem quebrada fica tentando subir task
    # indefinidamente e o servico degrada em silencio. Com rollback, ele volta
    # sozinho para a revisao anterior.
    enable   = true
    rollback = true
  }

  # Garante que a nova task passa no health check antes de a antiga sair.
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  # A primeira consulta de politica baixa 2,24GB de modelo (~5,8s medidos), e o
  # boot abre pool de conexao. Sem esta carencia, o load balancer marcaria a task
  # como insalubre antes de ela ficar pronta.
  health_check_grace_period_seconds = 120

  lifecycle {
    # A tag da imagem e alterada pelo CI, nao pelo Terraform. Sem isto, todo
    # `terraform apply` reverteria o deploy para a tag do arquivo de variaveis.
    ignore_changes = [task_definition]
  }
}
