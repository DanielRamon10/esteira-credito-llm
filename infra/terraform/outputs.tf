# Outputs.
#
# Todos existem porque o CI ou o operador precisa deles — output que ninguem
# consome e ruido que aparece a cada `apply`.

output "ecr_url" {
  description = "URL do repositorio, usada pelo CI no docker push."
  value       = aws_ecr_repository.api.repository_url
}

output "bucket_documentos" {
  description = "Bucket de documentos de cliente."
  value       = aws_s3_bucket.documentos.id
}

output "segredo_arn" {
  description = <<-TXT
    ARN do segredo da aplicacao.

    O ARN e publico por natureza; o valor nunca sai daqui — e nem entra, porque
    escrever a versao do segredo via Terraform o colocaria no estado em texto
    claro.
  TXT
  value       = aws_secretsmanager_secret.aplicacao.arn
}

output "cluster_ecs" {
  description = "Nome do cluster, para `aws ecs update-service` no deploy."
  value       = aws_ecs_cluster.principal.name
}

output "servico_ecs" {
  description = "Nome do servico, para forcar novo deployment a partir do CI."
  value       = aws_ecs_service.api.name
}

output "role_task_arn" {
  description = "Role que o codigo da aplicacao assume (S3). Separada da role de execucao de proposito."
  value       = aws_iam_role.task.arn
}
