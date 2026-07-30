output "ecr_url" {
  description = "URL do repositorio, usada pelo CI no docker push."
  value       = aws_ecr_repository.servico.repository_url
}

output "security_group_id" {
  description = <<-TXT
    SG do servico, para outro servico declarar que pode alcanca-lo.

    E o que permite `sgs_de_origem = [module.credit_analysis.security_group_id]` no
    `kyc-compliance` sem criar dependencia circular: o `credit-analysis` nao referencia o SG
    do KYC, porque o egress dele e aberto.
  TXT
  value       = aws_security_group.servico.id
}

output "nome_do_servico_ecs" {
  description = "Nome do servico no ECS, para `aws ecs update-service` no deploy."
  value       = aws_ecs_service.servico.name
}

output "role_task_arn" {
  description = "Role que o codigo da aplicacao assume. Separada da role de execucao de proposito."
  value       = aws_iam_role.task.arn
}

output "endereco_interno" {
  description = <<-TXT
    Nome DNS interno via Cloud Map, com a porta.

    Sai do modulo em vez de ser montado na raiz para que exista **uma** fonte do endereco.
    Foi um endereco ausente do ConfigMap que deixou o gate de conformidade rodando desligado
    nos manifests de Kubernetes; repetir a string em dois lugares e como o defeito volta.
  TXT
  value       = "http://${aws_service_discovery_service.servico.name}.${var.dominio_discovery}:${var.porta}"
}
