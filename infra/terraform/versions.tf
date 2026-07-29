# Versoes e estado remoto.
#
# ## Por que o backend esta comentado
#
# Estado do Terraform em arquivo local e a receita para dois problemas: ninguem
# mais consegue aplicar (o estado esta na maquina de quem aplicou) e o arquivo
# contem **valor de segredo em texto claro** — inclusive o que veio do Secrets
# Manager. Por isso o estado de verdade vai para S3 com versionamento e
# criptografia, e o lock impede dois `apply` simultaneos corrompendo tudo.
#
# Fica comentado porque um `terraform init` sem esse bucket falha, e este
# repositorio precisa poder ser validado por quem clonou sem ter conta AWS. O
# `terraform validate` do CI roda sem backend.
terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # backend "s3" {
  #   bucket = "esteira-credito-tfstate"
  #   key    = "credit-analysis/prod/terraform.tfstate"
  #   region = "sa-east-1"
  #
  #   # Lock nativo do S3 (provider 6.x), sem DynamoDB. Antes disso era preciso
  #   # uma tabela separada so para o lock.
  #   use_lockfile = true
  #   encrypt      = true
  # }
}

provider "aws" {
  region = var.regiao

  default_tags {
    # Tag em tudo, e nao por recurso: sem isso, rastrear custo por servico numa
    # conta compartilhada vira arqueologia de ARN. `Terraform = true` distingue o
    # que e gerenciado do que alguem criou pelo console as pressas.
    tags = {
      Projeto   = "esteira-credito"
      Servico   = "credit-analysis"
      Ambiente  = var.ambiente
      Terraform = "true"
    }
  }
}
