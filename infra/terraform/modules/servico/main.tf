# Modulo: um servico sem estado em ECS Fargate.
#
# ## Por que um modulo local, e nao tres arquivos parecidos
#
# Antes deste modulo, `main.tf` provisionava o `credit-analysis` direto. Com o segundo e o
# terceiro servico havia duas saidas: copiar o arquivo tres vezes, ou parametrizar.
#
# Copiar seria o mesmo erro que a extracao de `packages/plataforma` corrigiu no codigo
# Python: a parte sutil se multiplica e depois divergem sem ninguem decidir. Aqui a parte
# sutil e concreta e ja tem historico — a separacao entre role de **execucao** (usada pelo
# agente do ECS antes de o container subir) e role de **task** (usada pelo codigo em
# execucao). Juntar as duas e o erro mais comum em ECS, e ele nao quebra nada: funciona
# perfeitamente, com o codigo da aplicacao podendo ler qualquer segredo do servico.
#
# ## Por que tres blocos `module` explicitos, e nao `for_each`
#
# Um `for_each` sobre um mapa de tres servicos e mais curto, e esconderia justamente o que
# importa: o `credit-analysis` tem bucket de documento e segredo, o `kyc-compliance` nao
# aceita trafego da VPC, o `customer-support` e o unico voltado ao publico. Esses tres
# fatos ficam legiveis em blocos separados e viram condicional dentro de um `for_each`.
#
# O criterio: `for_each` quando as instancias diferem em **valor**, blocos separados quando
# diferem em **forma**.


# ---------------------------------------------------------------------- ECR

resource "aws_ecr_repository" "servico" {
  name = var.nome

  # `IMMUTABLE`: impede sobrescrever uma tag ja publicada. Sem isso, `v1.2.3` pode passar a
  # apontar para outro conteudo, e o rollback deixa de ser confiavel — o que e exatamente
  # quando se precisa dele.
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "servico" {
  repository = aws_ecr_repository.servico.name

  # Sem politica de ciclo de vida, um repositorio com deploy diario custa mais de
  # armazenamento que a computacao do servico em um ano. Vale para os tres, mas a conta e
  # bem diferente entre eles: a imagem do `credit-analysis` tem 1,18GB de dependencia
  # nativa (OpenCV, PyMuPDF, ONNX Runtime), contra 253MB do `kyc-compliance`.
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


# ---------------------------------------------------------------------- IAM

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
  name               = "${var.prefixo}-${var.nome}-execucao"
  assume_role_policy = data.aws_iam_policy_document.assumir_ecs.json
}

resource "aws_iam_role_policy_attachment" "execucao_padrao" {
  role       = aws_iam_role.execucao.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "ler_segredo" {
  # `count` e nao um documento sempre presente: uma politica com `resources = []` e
  # invalida, e os dois servicos novos nao tem segredo nenhum. Gerar um documento vazio
  # falharia no `apply`, nao no `validate` — o pior momento para descobrir.
  count = length(var.arns_de_segredo_legiveis) > 0 ? 1 : 0

  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = var.arns_de_segredo_legiveis
  }
}

resource "aws_iam_role_policy" "execucao_segredo" {
  count  = length(var.arns_de_segredo_legiveis) > 0 ? 1 : 0
  name   = "ler-segredo"
  role   = aws_iam_role.execucao.id
  policy = data.aws_iam_policy_document.ler_segredo[0].json
}

resource "aws_iam_role" "task" {
  name               = "${var.prefixo}-${var.nome}-task"
  assume_role_policy = data.aws_iam_policy_document.assumir_ecs.json
}

# A role da task existe mesmo sem politica anexada, e isso e proposital: `kyc-compliance` e
# `customer-support` nao acessam servico nenhum da AWS em execucao. Uma role sem permissao e
# a declaracao explicita de "este codigo nao precisa de nada", e e o que faz um `apply`
# futuro que adicione permissao aparecer no diff.
resource "aws_iam_role_policy" "aplicacao" {
  for_each = var.politicas_da_aplicacao

  name   = each.key
  role   = aws_iam_role.task.id
  policy = each.value
}


# ------------------------------------------------------------------ Execucao

resource "aws_cloudwatch_log_group" "servico" {
  name              = "/ecs/${var.prefixo}/${var.nome}"
  retention_in_days = var.dias_retencao_log
}

resource "aws_security_group" "servico" {
  name        = "${var.prefixo}-${var.nome}"
  description = "Trafego do servico ${var.nome}"
  vpc_id      = var.vpc_id
}

# Regras como recursos separados, e nao blocos `ingress`/`egress` embutidos.
#
# Nao e estilo: o AWS provider **proibe** misturar os dois no mesmo security group, e as
# regras aqui precisam ser condicionais (`kyc-compliance` nao aceita trafego da VPC) e
# iteradas (lista de SGs de origem). Bloco embutido nao faz nenhuma das duas coisas.
resource "aws_vpc_security_group_ingress_rule" "da_vpc" {
  count = var.ingress_da_vpc ? 1 : 0

  security_group_id = aws_security_group.servico.id
  description       = "HTTP da CIDR da VPC"
  # CIDR da VPC e nao 0.0.0.0/0: quem expoe para a internet e o load balancer, que tem o
  # proprio security group. Task acessivel direto da internet contorna WAF, log de acesso e
  # terminacao TLS.
  cidr_ipv4   = var.cidr_da_vpc
  from_port   = var.porta
  to_port     = var.porta
  ip_protocol = "tcp"
}

# As regras **entre** servicos nao moram aqui, e a razao e um ciclo real.
#
# A tentativa natural seria um `sgs_de_origem = [module.credit_analysis.security_group_id]`
# no modulo do `kyc-compliance`. Mas o `credit-analysis` precisa da URL do KYC na variavel
# de ambiente, ou seja depende de um output do modulo do KYC — e o KYC passaria a depender de
# um output do modulo do `credit-analysis`. Terraform resolve dependencia entre blocos
# `module` como um todo, entao isso e um ciclo, e ele falha no `validate`.
#
# A regra vive na raiz (`main.tf`), como recurso proprio referenciando os dois SGs. Nenhum
# dos dois modulos depende do outro pela rede, e a topologia de quem-fala-com-quem fica
# legivel num unico lugar — que e onde se quer ler isso.

resource "aws_vpc_security_group_egress_rule" "tudo" {
  security_group_id = aws_security_group.servico.id
  description       = "Saida para ECR, CloudWatch, Secrets Manager e dependencias"
  # Egress aberto e uma concessao consciente, e o contraste com Kubernetes e o ponto:
  # em `infra/k8s/` o egress e fechado por namespace, e chegou a ser **verificado** num
  # cluster real — o `customer-support` nao alcanca o `kyc-compliance`, e a fronteira de
  # divulgacao vale na rede e nao apenas no codigo.
  #
  # Fechar aqui exigiria VPC endpoints para ECR, S3, Secrets Manager e CloudWatch, mais
  # regras entre security groups. Fica como divida registrada, nao como equivalencia
  # sugerida: as duas infraestruturas **nao** oferecem a mesma garantia hoje.
  cidr_ipv4   = "0.0.0.0/0"
  ip_protocol = "-1"
}


# ------------------------------------------------------- Descoberta de servico

# Cloud Map, e ele existe por um motivo especifico deste commit.
#
# Em Kubernetes o Service da um nome DNS de graca, e foi exatamente isso que expos o defeito
# corrigido nos manifests: o `CREDIT_KYC_URL` estava ausente do ConfigMap e o gate de
# conformidade rodava desligado em silencio. Em ECS nao ha equivalente automatico — sem
# Cloud Map, o `credit-analysis` nao tem nome nenhum para chamar, e o mesmo defeito
# reapareceria aqui em outra forma: URL apontando para IP de task, que muda a cada deploy.
resource "aws_service_discovery_service" "servico" {
  name = var.nome

  dns_config {
    namespace_id = var.namespace_discovery_id

    dns_records {
      # `A` e nao `SRV`: as tasks usam `awsvpc`, logo cada uma tem IP proprio e a porta e
      # conhecida em tempo de configuracao. `SRV` seria necessario com bridge networking,
      # onde a porta e sorteada.
      ttl  = 10
      type = "A"
    }

    routing_policy = "MULTIVALUE"
  }

  # Bloco `custom` **vazio**, e nao um health check do Route 53.
  #
  # `custom` significa que quem decide sobre saude e o proprio ECS, pelo health check do
  # container. Um health check do Route 53 aqui checaria de fora da VPC e duplicaria a
  # decisao — com as duas discordando em algum momento, e o DNS mantendo no ar uma task que o
  # ECS considera morta.
  #
  # Vazio porque `failure_threshold` esta deprecado: a AWS deixou de suportar o argumento e o
  # valor e sempre 1. Deixa-lo produzia aviso no `validate`, e aviso ignorado no CI e como
  # alerta ignorado no painel.
  health_check_custom_config {}
}


resource "aws_ecs_task_definition" "servico" {
  family                   = "${var.prefixo}-${var.nome}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.cpu
  memory                   = var.memoria
  execution_role_arn       = aws_iam_role.execucao.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([
    {
      name      = "api"
      essential = true
      image     = "${aws_ecr_repository.servico.repository_url}:${var.tag_imagem}"

      portMappings = [
        {
          containerPort = var.porta
          protocol      = "tcp"
        }
      ]

      environment = [
        for chave, valor in var.variaveis : { name = chave, value = valor }
      ]

      secrets = [
        for chave, origem in var.segredos : { name = chave, valueFrom = origem }
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.servico.name
          "awslogs-region"        = var.regiao
          "awslogs-stream-prefix" = "api"
        }
      }

      healthCheck = {
        # `/health` e nao `/ready`, e a distincao nao e cosmetica: este check decide se o
        # container esta **vivo**; quem decide sobre trafego e o load balancer, pelo
        # `/ready`.
        #
        # Nos dois servicos novos isso pesa mais do que parece. O `/ready` do
        # `kyc-compliance` reprova com lista vazia e o do `customer-support` reprova sem
        # artigo publico — sao as condicoes em que o servico esta vivo e inutil. Usar
        # `/ready` aqui transformaria cada uma delas em reinicio de container, que nao
        # carrega lista nem artigo nenhum.
        command     = ["CMD-SHELL", "curl -fsS http://localhost:${var.porta}/health || exit 1"]
        interval    = 15
        timeout     = 3
        retries     = 3
        startPeriod = 30
      }
    }
  ])
}

resource "aws_ecs_service" "servico" {
  name            = "${var.prefixo}-${var.nome}"
  cluster         = var.cluster_id
  task_definition = aws_ecs_task_definition.servico.arn
  desired_count   = var.replicas
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = var.subnet_ids
    security_groups = [aws_security_group.servico.id]
    # `true` porque as tasks estao em subnet publica da VPC default e precisam alcancar ECR
    # e Secrets Manager. Em subnet privada com NAT isto vira `false` — e essa e a
    # configuracao correta para producao.
    assign_public_ip = true
  }

  service_registries {
    registry_arn = aws_service_discovery_service.servico.arn
  }

  deployment_circuit_breaker {
    # Sem circuit breaker, um deploy de imagem quebrada fica tentando subir task
    # indefinidamente e o servico degrada em silencio. Com rollback, ele volta sozinho para
    # a revisao anterior.
    enable   = true
    rollback = true
  }

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  health_check_grace_period_seconds = var.carencia_health_check

  lifecycle {
    # A tag da imagem e alterada pelo CI, nao pelo Terraform. Sem isto, todo `terraform
    # apply` reverteria o deploy para a tag do arquivo de variaveis.
    ignore_changes = [task_definition]
  }
}
