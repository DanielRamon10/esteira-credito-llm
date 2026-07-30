# Outputs.
#
# Todos existem porque o CI ou o operador precisa deles — output que ninguem consome e ruido
# que aparece a cada `apply`.
#
# Os que sao por servico saem como mapa em vez de nove outputs soltos: o `deploy.yml` itera
# sobre eles, e nove nomes com sufixo (`ecr_url_kyc`, `ecr_url_suporte`, ...) exigiriam o
# workflow conhecer cada um. Com mapa, adicionar um quarto servico nao mexe no CI.

output "urls_ecr" {
  description = "Repositorio de cada servico, para o `docker push` do CI."
  value = {
    credit-analysis  = module.credit_analysis.ecr_url
    kyc-compliance   = module.kyc_compliance.ecr_url
    customer-support = module.customer_support.ecr_url
  }
}

output "servicos_ecs" {
  description = "Nome de cada servico no ECS, para `aws ecs update-service --force-new-deployment`."
  value = {
    credit-analysis  = module.credit_analysis.nome_do_servico_ecs
    kyc-compliance   = module.kyc_compliance.nome_do_servico_ecs
    customer-support = module.customer_support.nome_do_servico_ecs
  }
}

output "cluster_ecs" {
  description = "Cluster compartilhado pelos tres."
  value       = aws_ecs_cluster.principal.name
}

output "enderecos_internos" {
  description = <<-TXT
    Nome DNS interno de cada servico, via Cloud Map.

    Util para conferir num `terraform output` que o `CREDIT_KYC_URL` injetado na task aponta
    para onde se espera — a verificacao que faltou no ConfigMap do Kubernetes, onde a variavel
    simplesmente nao existia e o gate de conformidade rodava desligado em silencio.
  TXT
  value = {
    credit-analysis  = module.credit_analysis.endereco_interno
    kyc-compliance   = module.kyc_compliance.endereco_interno
    customer-support = module.customer_support.endereco_interno
  }
}

output "bucket_documentos" {
  description = "Bucket de documentos de cliente. Apenas o credit-analysis tem um."
  value       = aws_s3_bucket.documentos.id
}

output "segredo_credit_analysis_arn" {
  description = <<-TXT
    ARN do segredo do credit-analysis, o unico dos tres que tem credencial.

    O ARN e publico por natureza; o valor nunca sai daqui — e nem entra, porque escrever a
    versao do segredo via Terraform o colocaria no estado em texto claro.
  TXT
  value       = aws_secretsmanager_secret.credit_analysis.arn
}

output "roles_de_task" {
  description = <<-TXT
    Role que o codigo de cada servico assume em execucao, separada da role de execucao.

    As do KYC e do atendimento existem **sem politica anexada**, e isso e a declaracao de que
    aqueles processos nao precisam de nada da AWS. Uma permissao adicionada no futuro aparece
    no diff em vez de passar como detalhe de uma role que ja tinha coisas.
  TXT
  value = {
    credit-analysis  = module.credit_analysis.role_task_arn
    kyc-compliance   = module.kyc_compliance.role_task_arn
    customer-support = module.customer_support.role_task_arn
  }
}
