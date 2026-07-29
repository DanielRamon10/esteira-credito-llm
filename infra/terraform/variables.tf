variable "regiao" {
  description = "Regiao da AWS. sa-east-1 por residencia de dado: documento de credito de cliente brasileiro sob LGPD nao deve sair do pais sem necessidade."
  type        = string
  default     = "sa-east-1"
}

variable "ambiente" {
  description = "Ambiente logico (dev, staging, prod)."
  type        = string
  default     = "prod"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.ambiente)
    error_message = "Ambiente deve ser dev, staging ou prod."
  }
}

variable "nome" {
  description = "Prefixo dos recursos."
  type        = string
  default     = "credit-analysis"
}

variable "tag_imagem" {
  description = <<-TXT
    Tag da imagem no ECR. Preenchida pelo CI com o SHA do commit.

    Nunca `latest`: com tag mutavel, duas tasks do mesmo servico podem rodar
    codigo diferente conforme o momento em que baixaram a imagem, e o sintoma e
    um bug que aparece em parte das requisicoes. Tambem impossibilita rollback
    por tag.
  TXT
  type        = string
  default     = "PREENCHIDO_PELO_CI"
}

variable "cpu_task" {
  description = "CPU da task Fargate (unidades). 1024 = 1 vCPU."
  type        = number
  default     = 2048
}

variable "memoria_task" {
  description = <<-TXT
    Memoria da task em MiB.

    4096 e nao menos: o modelo de embedding ocupa ~2,3GB residentes depois de
    carregado, e Fargate mata a task por OOM sem aviso util. Sobra folga para o
    buffer de OCR, que carrega imagem inteira em memoria.
  TXT
  type        = number
  default     = 4096
}

variable "replicas_desejadas" {
  description = "Quantidade de tasks em regime."
  type        = number
  default     = 2
}

variable "dias_retencao_log" {
  description = <<-TXT
    Retencao do log no CloudWatch.

    30 dias e um meio-termo consciente: log de esteira de credito e material de
    auditoria, mas tambem contem `analise_id` e valor de proposta. Reter para
    sempre aumenta a exposicao sem ganho proporcional; o que precisa de retencao
    longa e o parecer no banco, nao a linha de log.
  TXT
  type        = number
  default     = 30
}
