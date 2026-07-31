# Infraestrutura da esteira de credito na AWS: tres servicos em ECS Fargate.
#
# ## Fargate e nao EC2, e por que existem tambem manifests de Kubernetes
#
# Este diretorio provisiona ECS Fargate. Os manifests em `infra/k8s/` sao o **caminho
# alternativo**, para quem ja tem cluster — e nao um segundo ambiente. A duplicacao e
# proposital e vale explicar em vez de esconder: a escolha entre os dois nao e tecnica em
# abstrato, e sim "ja existe cluster e time de plataforma?". Sem cluster, Fargate elimina
# gestao de no; com cluster, subir um runtime separado so para tres servicos e desperdicio.
#
# EC2 ficaria de fora nos dois casos: patch de sistema operacional, autoscaling group e AMI
# dourada sao trabalho que nao agrega nada a APIs sem estado.
#
# **As duas infraestruturas nao sao equivalentes hoje**, e o ponto onde divergem esta
# registrado no modulo: em Kubernetes o egress e fechado por namespace e foi verificado num
# cluster real; aqui ele e aberto, porque fechar exigiria VPC endpoints para ECR, S3, Secrets
# Manager e CloudWatch. E divida, nao paridade.
#
# ## Rede: VPC default por escolha de escopo
#
# Uma VPC propria (subnets publicas e privadas em tres AZs, NAT, tabelas de rota) sao ~150
# linhas que nao ensinam nada sobre estes servicos. **Em producao de verdade isto nao
# serve**: as tasks precisam ficar em subnet privada com NAT, e o comentario fica aqui para
# que a limitacao seja lida, e nao descoberta.

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
  prefixo = "${var.nome_do_projeto}-${var.ambiente}"

  # Variaveis que os tres servicos compartilham. Repetir em cada bloco garantiria que um dia
  # dois deles logam em formato diferente sem ninguem ter decidido isso.
  comuns = {
    LOG_JSON         = "true"
    NIVEL_LOG        = "INFO"
    DOCS_HABILITADOS = "false" # a doc interativa expoe o schema completo, que e reconhecimento

    # Autenticacao (Camada 7). **JWKS aqui, e nao arquivo montado como no Kubernetes**, e a
    # diferenca nao e preferencia:
    #
    # - o ECS Fargate injeta segredo como **variavel de ambiente**, nao como arquivo. Montar um
    #   PEM exigiria EFS ou um sidecar que o escreve, ou seja infraestrutura para transportar
    #   uma chave publica;
    # - JWKS resolve isso melhor de qualquer forma: rotacao sem tocar na task definition, e sem
    #   uma variavel de ambiente com PEM aparecendo inteira num `describe-task-definition`.
    #
    # O Kubernetes usa arquivo porque la o Secret montado como volume e propagado pelo kubelet
    # em segundos — vantagem que o Fargate nao oferece.
    AUTH_JWKS_URL = var.jwks_url
    AUTH_EMISSOR  = var.emissor_de_token
  }
}


# ------------------------------------------------------------- Compartilhado

resource "aws_ecs_cluster" "principal" {
  # Um cluster para os tres. Em Fargate o cluster e apenas um agrupamento logico: nao ha no
  # para gerenciar e ele nao custa nada. Tres clusters dariam tres lugares para olhar metrica
  # de um sistema que e um.
  name = local.prefixo

  setting {
    # Container Insights custa e paga por si no primeiro incidente: sem ele, nao ha metrica
    # de CPU e memoria por task, so agregado de cluster.
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_service_discovery_private_dns_namespace" "interno" {
  # O equivalente, em ECS, do DNS que um Service do Kubernetes da de graca.
  #
  # Foi a **ausencia** desse endereco no ConfigMap do Kubernetes que deixou o gate de
  # conformidade rodando desligado em silencio: `CREDIT_KYC_URL` vazio faz o servico montar o
  # cliente fake e a esteira aprovar sem consultar lista de sancoes. Aqui o endereco sai de um
  # output do modulo, entao nao ha string para esquecer.
  name        = var.dominio_interno
  description = "Descoberta interna entre os servicos da esteira"
  vpc         = data.aws_vpc.default.id
}


# ------------------------------- Armazenamento (apenas o credit-analysis)
#
# So este servico tem bucket, e a assimetria e conteudo: o `kyc-compliance` compara listas
# assadas na imagem e o `customer-support` serve artigos que vem na imagem. Criar bucket "por
# simetria" para os dois daria a cada um uma superficie de vazamento que eles nao precisam ter.

resource "aws_s3_bucket" "documentos" {
  # Nome com account id porque nome de bucket e global: sem sufixo, um `apply` em outra conta
  # colide com um bucket que nao e seu.
  bucket = "${local.prefixo}-documentos-${data.aws_caller_identity.atual.account_id}"
}

resource "aws_s3_bucket_public_access_block" "documentos" {
  # Primeiro recurso a escrever, e nao um detalhe: este bucket guarda holerite e extrato
  # bancario. Vazamento de bucket de documento e o incidente mais comum e mais caro que existe
  # em nuvem.
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
    # Versionamento protege contra sobrescrita e delete acidental. Num bucket que sustenta
    # decisao de credito, "o documento que embasou este parecer" precisa continuar
    # recuperavel mesmo depois de um upload errado.
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "documentos" {
  bucket = aws_s3_bucket.documentos.id

  rule {
    id     = "expirar-documento-processado"
    status = "Enabled"

    filter {}

    # LGPD art. 15: dado pessoal nao deve ser guardado alem da finalidade. O documento serve
    # para apurar renda; passada a operacao, o que precisa sobreviver e o parecer e a trilha,
    # nao a imagem do holerite.
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


# ---------------------------------- Segredos (apenas o credit-analysis)

resource "aws_secretsmanager_secret" "credit_analysis" {
  name        = "${local.prefixo}/credit-analysis"
  description = "DSN do Postgres do credit-analysis."

  # Zero dias: permite recriar o segredo com o mesmo nome sem esperar a janela de
  # recuperacao. Em producao com dado critico o valor correto seria 7 a 30 dias — aqui o
  # trade-off pende para iteracao.
  recovery_window_in_days = 0
}

# Nao ha `aws_secretsmanager_secret_version` neste arquivo, e a ausencia e deliberada.
# Escrever o valor aqui o colocaria no **estado** do Terraform em texto claro, e o estado e um
# arquivo que vai para S3 e e lido por quem tem acesso ao bucket. O valor entra fora do
# Terraform (console, CLI ou rotacao automatica); daqui sai apenas o ARN.

data "aws_iam_policy_document" "documentos" {
  statement {
    actions = ["s3:GetObject", "s3:PutObject"]
    # Objetos dentro do bucket, nao o bucket. Sem o `/*` a permissao nao funciona; com o ARN
    # do bucket incluido, `s3:PutObject` viraria permissao sobre o proprio bucket.
    resources = ["${aws_s3_bucket.documentos.arn}/*"]
  }

  statement {
    # `ListBucket` separado porque age no bucket e nao no objeto. Sem o prefixo, a aplicacao
    # poderia listar o bucket inteiro.
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.documentos.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["documentos/*"]
    }
  }
}

# Sem `s3:DeleteObject` na role da aplicacao, de proposito: a expiracao e feita pela regra de
# ciclo de vida do bucket, que e auditavel e nao depende de codigo. Aplicacao que pode apagar
# documento pode apagar a evidencia de um parecer.


# ------------------------------------------------------------------ Servicos

module "credit_analysis" {
  source = "./modules/servico"

  nome    = "credit-analysis"
  porta   = 8000
  prefixo = local.prefixo

  regiao            = var.regiao
  ambiente          = var.ambiente
  tag_imagem        = var.tag_imagem
  vpc_id            = data.aws_vpc.default.id
  subnet_ids        = data.aws_subnets.default.ids
  cluster_id        = aws_ecs_cluster.principal.id
  dias_retencao_log = var.dias_retencao_log

  namespace_discovery_id = aws_service_discovery_private_dns_namespace.interno.id
  dominio_discovery      = var.dominio_interno

  # 4096 MiB e nao menos: o modelo de embedding ocupa ~2,3GB residentes depois de carregado, e
  # Fargate mata a task por OOM sem aviso util. Sobra folga para o buffer de OCR, que carrega
  # imagem inteira em memoria.
  cpu      = 2048
  memoria  = 4096
  replicas = var.replicas.credit_analysis

  # 120s: a primeira consulta de politica baixa 2,24GB de modelo (~5,8s medidos) e o boot abre
  # pool de conexao. Sem esta carencia, o load balancer marcaria a task como insalubre antes
  # de ela ficar pronta.
  carencia_health_check = 120

  ingress_da_vpc = true
  cidr_da_vpc    = data.aws_vpc.default.cidr_block

  variaveis = merge(
    { for chave, valor in local.comuns : "CREDIT_${chave}" => valor },
    {
      CREDIT_AMBIENTE = var.ambiente
      # Explicito e nao `auto`: sem provedor disponivel o servico falha ao subir, em vez de
      # responder parecer com texto de um fake. O `customer-support` faz o oposto, e o
      # contraste esta explicado no bloco dele.
      CREDIT_PROVEDOR_LLM      = "ollama"
      CREDIT_MODELO_OLLAMA     = "llama3.1:8b"
      CREDIT_MODELO_AGENTE     = "qwen2.5:7b"
      CREDIT_BUCKET_DOCUMENTOS = aws_s3_bucket.documentos.id
      # Vem do output do modulo, e nao de uma string escrita a mao. Foi este endereco, ausente
      # do ConfigMap do Kubernetes, que deixou o gate de conformidade rodando desligado.
      CREDIT_KYC_URL = module.kyc_compliance.endereco_interno
      # Endpoint de token do IdP: o `credit-analysis` obtem credencial propria para o KYC via
      # `client_credentials`. O token dele (`aud=credit-analysis`) nao pode ser repassado —
      # seria a escalada lateral que a validacao de audiencia existe para impedir.
      CREDIT_KYC_TOKEN_URL = var.token_url
      CREDIT_KYC_CLIENT_ID = var.kyc_client_id
    },
  )

  segredos = {
    CREDIT_POSTGRES_DSN = "${aws_secretsmanager_secret.credit_analysis.arn}:CREDIT_POSTGRES_DSN::"
    # Por referencia, como o DSN. Em `variaveis` ele apareceria num
    # `aws ecs describe-task-definition`, que qualquer role com leitura de ECS consegue chamar —
    # e o `client_secret` e o que permite **emitir** token em nome deste servico.
    CREDIT_KYC_CLIENT_SECRET = "${aws_secretsmanager_secret.credit_analysis.arn}:CREDIT_KYC_CLIENT_SECRET::"
  }

  arns_de_segredo_legiveis = [aws_secretsmanager_secret.credit_analysis.arn]
  politicas_da_aplicacao   = { documentos = data.aws_iam_policy_document.documentos.json }
}

module "kyc_compliance" {
  source = "./modules/servico"

  nome    = "kyc-compliance"
  porta   = 8100
  prefixo = local.prefixo

  regiao            = var.regiao
  ambiente          = var.ambiente
  tag_imagem        = var.tag_imagem
  vpc_id            = data.aws_vpc.default.id
  subnet_ids        = data.aws_subnets.default.ids
  cluster_id        = aws_ecs_cluster.principal.id
  dias_retencao_log = var.dias_retencao_log

  namespace_discovery_id = aws_service_discovery_private_dns_namespace.interno.id
  dominio_discovery      = var.dominio_interno

  # A menor task dos tres, e a diferenca e o argumento pratico do custo de um modelo: este
  # servico nao carrega modelo, nao fala com banco e nao chama LLM. A triagem e comparacao de
  # string em memoria contra listas assadas na imagem.
  cpu      = 512
  memoria  = 1024
  replicas = var.replicas.kyc_compliance

  # 30s: o boot le CSV do proprio filesystem. Dar mais tempo do que o boot precisa atrasa a
  # deteccao de uma task que subiu quebrada.
  carencia_health_check = 30

  # **`false`, e nao e esquecimento.** Este servico nao e API publica, e nem sequer aceita
  # trafego de toda a VPC: a unica regra de ingress esta na secao de rede abaixo, e libera
  # somente o `credit-analysis`. Triagem de PEP consultavel por qualquer coisa na VPC e um
  # oraculo para descobrir quem esta em lista restritiva.
  ingress_da_vpc = false

  variaveis = merge(
    { for chave, valor in local.comuns : "KYC_${chave}" => valor },
    {
      KYC_AMBIENTE = var.ambiente
      # Caminho relativo ao WORKDIR `/app`, onde o Dockerfile copia `dados/`. As listas
      # versionadas sao sinteticas; em producao de verdade vem do COAF e das listas de
      # sancoes, por um passo que baixa e valida antes de a task subir.
      KYC_DIRETORIO_LISTAS = "dados/listas"
    },
  )

  # Sem `segredos`, sem `arns_de_segredo_legiveis` e sem `politicas_da_aplicacao`. As tres
  # ausencias sao a declaracao de que este codigo nao precisa de nada da AWS em execucao — e e
  # o que faz um `apply` futuro que adicione permissao aparecer no diff.
}

module "customer_support" {
  source = "./modules/servico"

  nome    = "customer-support"
  porta   = 8200
  prefixo = local.prefixo

  regiao            = var.regiao
  ambiente          = var.ambiente
  tag_imagem        = var.tag_imagem
  vpc_id            = data.aws_vpc.default.id
  subnet_ids        = data.aws_subnets.default.ids
  cluster_id        = aws_ecs_cluster.principal.id
  dias_retencao_log = var.dias_retencao_log

  namespace_discovery_id = aws_service_discovery_private_dns_namespace.interno.id
  dominio_discovery      = var.dominio_interno

  # Mais que o KYC e muito menos que o credit-analysis: a busca e BM25 puro (stdlib), e a
  # medicao que justificou dispensar embedding foi 92% de acerto em top-1 e 100% em top-3
  # sobre a base real. O que resta e o buffer das chamadas ao Ollama.
  cpu      = 1024
  memoria  = 2048
  replicas = var.replicas.customer_support

  carencia_health_check = 30

  # O unico dos tres voltado ao publico: e o canal de atendimento que chama esta API.
  ingress_da_vpc = true
  cidr_da_vpc    = data.aws_vpc.default.cidr_block

  variaveis = merge(
    { for chave, valor in local.comuns : "SUP_${chave}" => valor },
    {
      SUP_AMBIENTE = var.ambiente
      # `auto`, e aqui esta a diferenca mais importante em relacao ao credit-analysis.
      #
      # La o provedor e `ollama` explicito, porque com `auto` um Ollama fora do ar faria o
      # servico cair no fake e devolver parecer de credito com texto inventado — melhor nao
      # subir. Aqui o fallback e outro: sem Ollama, a resposta e **o texto do artigo revisado
      # por gente**. Entre entregar um artigo cru ao cliente e nao atender, o artigo ganha, e
      # o campo `origem` no contrato diz ao canal qual caminho produziu o texto.
      #
      # Verificado num cluster: sem Ollama alcancavel, o servico sobe Ready com
      # `llm: "artigo"`.
      SUP_PROVEDOR_LLM = "auto"
      # Modelo pequeno de proposito: a tarefa e reescrever um artigo curto em linguagem
      # simples, e o cliente esta esperando.
      SUP_MODELO_OLLAMA           = "llama3.2:3b"
      SUP_DIRETORIO_CONHECIMENTO  = "conhecimento"
      SUP_OLLAMA_TIMEOUT_SEGUNDOS = "60"
    },
  )
}


# --------------------------------------------------------------------- Rede
#
# Quem fala com quem, num unico lugar. As regras entre servicos nao podem morar dentro do
# modulo: o `credit-analysis` depende de um output do modulo do KYC (o endereco de
# descoberta), entao o KYC nao pode depender de um output do modulo do `credit-analysis` —
# Terraform resolve dependencia entre blocos `module` como um todo, e isso seria um ciclo.
#
# O efeito colateral e bom: a topologia fica legivel aqui, em vez de espalhada por tres
# instanciacoes.

resource "aws_vpc_security_group_ingress_rule" "kyc_recebe_do_credit_analysis" {
  security_group_id            = module.kyc_compliance.security_group_id
  description                  = "Gate de conformidade: somente o credit-analysis consulta o KYC"
  referenced_security_group_id = module.credit_analysis.security_group_id
  from_port                    = 8100
  to_port                      = 8100
  ip_protocol                  = "tcp"
}

# **Nao ha regra permitindo o `customer-support` alcancar os outros dois**, e a ausencia e
# conteudo.
#
# O servico tem tres defesas de aplicacao contra revelar limiar interno ao cliente — filtro de
# visibilidade na entrada, guard na saida, e roteamento deterministico fora do prompt — e
# todas as tres sao quebraveis por refatoracao. A ausencia de rota nao e. E a diferenca entre
# "o codigo nao faz isso" e "isso nao e possivel".
#
# O egress aberto do security group enfraquece a garantia aqui, e por isso ela nao e afirmada
# como equivalente: em `infra/k8s/` o egress e fechado por namespace e o bloqueio foi
# **verificado** num cluster real. Fechar o equivalente em ECS depende dos VPC endpoints
# citados no modulo. Se um dia alguem precisar dessa rota, a discussao passa obrigatoriamente
# por editar este arquivo, que e onde ela deve acontecer.
