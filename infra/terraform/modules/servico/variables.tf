variable "nome" {
  description = "Nome do servico. Vira nome do repositorio ECR, da familia da task e do log group."
  type        = string
}

variable "prefixo" {
  description = "Prefixo `<nome-do-projeto>-<ambiente>`, vindo da raiz."
  type        = string
}

variable "porta" {
  description = "Porta que o container escuta. Difere por servico (8000, 8100, 8200)."
  type        = number
}

variable "regiao" {
  type = string
}

variable "ambiente" {
  type = string
}

variable "tag_imagem" {
  description = "Tag da imagem no ECR, preenchida pelo CI com o SHA do commit."
  type        = string
}

variable "cpu" {
  description = "CPU da task Fargate (1024 = 1 vCPU)."
  type        = number
}

variable "memoria" {
  description = "Memoria da task em MiB."
  type        = number
}

variable "replicas" {
  type = number
}

variable "vpc_id" {
  type = string
}

variable "subnet_ids" {
  type = list(string)
}

variable "cluster_id" {
  description = <<-TXT
    Cluster ECS compartilhado, criado na raiz.

    Um cluster por servico seria a copia literal do que existia antes da modularizacao, e
    nao compra nada: em Fargate o cluster e apenas um agrupamento logico, sem no para
    gerenciar e sem custo proprio. Tres clusters significariam tres lugares para olhar
    metrica de um sistema que e um.
  TXT
  type        = string
}

variable "dias_retencao_log" {
  type = number
}

variable "variaveis" {
  description = <<-TXT
    Variaveis de ambiente nao sensiveis.

    Um `map` em vez da lista de objetos que o ECS espera: a conversao acontece no modulo, e
    quem chama escreve `{ KYC_AMBIENTE = "prod" }` em vez de repetir `{ name = ..., value =
    ... }` em cada linha.
  TXT
  type        = map(string)
  default     = {}
}

variable "segredos" {
  description = <<-TXT
    Segredos por **referencia**: nome da variavel de ambiente -> ARN com a chave do JSON.

    O ECS resolve no momento de subir o container, entao o valor nunca aparece na task
    definition — que e legivel por qualquer um com `ecs:DescribeTaskDefinition`.

    Vazio nos dois servicos novos, e a ausencia e conteudo: o `kyc-compliance` compara
    listas assadas na imagem e o `customer-support` fala com um Ollama sem autenticacao.
    Nenhum dos dois tem credencial para guardar.
  TXT
  type        = map(string)
  default     = {}
}

variable "arns_de_segredo_legiveis" {
  description = <<-TXT
    ARNs que a role de **execucao** pode ler.

    Separado de `segredos` porque a permissao e por segredo inteiro e a variavel aponta
    para uma chave dentro dele. Lista explicita, nunca `*`: uma role que le qualquer
    segredo da conta e escalada de privilegio esperando o momento.
  TXT
  type        = list(string)
  default     = []
}

variable "politicas_da_aplicacao" {
  description = <<-TXT
    Politicas anexadas a role da **task**, ou seja ao codigo em execucao: nome -> JSON.

    Continua separada da role de execucao. Juntar as duas daria ao codigo da aplicacao
    permissao de puxar imagem e ler qualquer segredo do servico — que e exatamente o que um
    comprometimento de aplicacao procura.
  TXT
  type        = map(string)
  default     = {}
}

variable "ingress_da_vpc" {
  description = <<-TXT
    Se `true`, aceita trafego de toda a CIDR da VPC.

    `false` no `kyc-compliance`: ele nao e API publica, e triagem de PEP consultavel por
    qualquer coisa dentro da VPC e um oraculo para descobrir quem esta em lista restritiva.
  TXT
  type        = bool
  default     = false
}

variable "cidr_da_vpc" {
  type    = string
  default = ""
}

variable "namespace_discovery_id" {
  description = "ID do namespace de DNS privado (Cloud Map) criado na raiz."
  type        = string
}

variable "dominio_discovery" {
  description = "Dominio do namespace (ex. `esteira.local`), para montar o endereco interno."
  type        = string
}

variable "carencia_health_check" {
  description = "Segundos de carencia antes de o ECS considerar a task insalubre."
  type        = number
  default     = 60
}
