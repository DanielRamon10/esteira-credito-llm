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

variable "nome_do_projeto" {
  description = <<-TXT
    Prefixo dos recursos compartilhados.

    Era `nome` com default `credit-analysis`, o que fazia sentido quando havia um servico. Com
    tres, um cluster chamado `credit-analysis-prod` hospedando tambem KYC e atendimento
    passaria a ser enganoso — nome de recurso e a primeira documentacao que alguem le num
    console de nuvem as duas da manha.
  TXT
  type        = string
  default     = "esteira-credito"
}

variable "dominio_interno" {
  description = <<-TXT
    Dominio do namespace de DNS privado (Cloud Map) usado entre os servicos.

    `.local` e nao um subdominio de dominio real: este namespace so existe dentro da VPC, e
    usar um nome roteavel convidaria alguem a tentar resolve-lo de fora e concluir que o
    servico esta fora do ar.
  TXT
  type        = string
  default     = "esteira.local"
}

variable "tag_imagem" {
  description = <<-TXT
    Tag da imagem no ECR, aplicada aos tres servicos. Preenchida pelo CI com o SHA do commit.

    Uma tag para os tres, e nao uma por servico: eles vivem no mesmo monorepo e sao promovidos
    juntos. Tags independentes permitiriam subir um `credit-analysis` que espera um contrato de
    KYC ainda nao implantado — e o contrato entre os dois nao tem versionamento.

    Nunca `latest`: com tag mutavel, duas tasks do mesmo servico podem rodar codigo diferente
    conforme o momento em que baixaram a imagem, e o sintoma e um bug que aparece em parte das
    requisicoes. Tambem impossibilita rollback por tag.
  TXT
  type        = string
  default     = "PREENCHIDO_PELO_CI"
}

variable "replicas" {
  description = <<-TXT
    Tasks em regime, por servico.

    Um objeto e nao tres variaveis soltas: as contagens sao lidas juntas quando alguem
    dimensiona o ambiente, e sao independentes por natureza — uma analise de credito nao gera
    um atendimento, e um atendimento nao gera uma triagem. Dimensionar os tres igual trataria
    como acoplado o que foi separado justamente por nao ser.
  TXT
  type = object({
    credit_analysis  = number
    kyc_compliance   = number
    customer_support = number
  })
  default = {
    credit_analysis = 2
    # 2 tambem no KYC, e nao 1: se ele sai do ar, o `credit-analysis` abre o disjuntor e manda
    # **toda** analise para revisao humana. Indisponibilidade dele nao derruba a esteira,
    # transforma ela em fila de analista.
    kyc_compliance = 2
    # O unico exposto ao publico, e o unico cujo volume nao segue o dos outros: uma campanha de
    # marketing ou uma noticia ruim geram pico aqui e nada nos demais.
    customer_support = 3
  }
}

variable "dias_retencao_log" {
  description = <<-TXT
    Retencao do log no CloudWatch, igual para os tres.

    30 dias e um meio-termo consciente: log de esteira de credito e material de auditoria, mas
    tambem contem `analise_id` e valor de proposta. Reter para sempre aumenta a exposicao sem
    ganho proporcional; o que precisa de retencao longa e o parecer no banco, nao a linha de
    log.
  TXT
  type        = number
  default     = 30
}

variable "emissor_de_token" {
  description = <<-TXT
    `iss` esperado nos tokens. Os tres servicos validam contra este valor.

    Um so para os tres, e nao um por servico: eles confiam no mesmo IdP corporativo. Valores
    diferentes significariam emissores diferentes, o que e uma decisao de arquitetura e nao de
    configuracao — e deveria aparecer como tal.
  TXT
  type        = string
  default     = "https://idp.interno.invalid"
}

variable "jwks_url" {
  description = <<-TXT
    JWKS do IdP, usado pelos tres para verificar assinatura.

    Preferido sobre PEM em variavel de ambiente por dois motivos: permite **rotacao de chave
    sem redeploy** (o emissor publica a nova e os servicos a veem no proximo ciclo de cache), e
    evita que o material de chave apareça num `describe-task-definition`.
  TXT
  type        = string
  default     = "https://idp.interno.invalid/.well-known/jwks.json"
}

variable "token_url" {
  description = "Endpoint de `client_credentials` do IdP, usado pelo credit-analysis para obter credencial ao chamar o KYC."
  type        = string
  default     = "https://idp.interno.invalid/oauth/token"
}

variable "kyc_client_id" {
  description = <<-TXT
    Client ID do `credit-analysis` no IdP, para o fluxo `client_credentials`.

    Somente o ID vem por variavel; o **secret** entra por Secrets Manager, porque em variavel de
    ambiente ele apareceria num `describe-task-definition` — e o secret e o que permite emitir
    token em nome deste servico.
  TXT
  type        = string
  default     = "credit-analysis"
}
