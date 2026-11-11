output "cluster_name" {
  value = local.cluster_name
}

output "cluster_endpoint" {
  value = local.cluster.endpoint
}

output "cluster_ca_certificate" {
  description = "Base64 PEM."
  value       = local.cluster.certificate_authority[0].data
}

output "update_kubeconfig_command" {
  value = "aws eks update-kubeconfig --name ${local.cluster_name} --region ${local.region}"
}

output "runtime_role_arn" {
  value = aws_iam_role.irsa["runtime"].arn
}

output "migrate_role_arn" {
  value = aws_iam_role.irsa["migrate"].arn
}

output "certificate_arn" {
  value = local.certificate_arn
}

output "certificate_validation_records" {
  description = "DNS records that prove domain ownership to ACM, when route53_zone_id was not given. Create them at your DNS provider."
  value = var.certificate_arn == null && var.route53_zone_id == null ? [
    for o in aws_acm_certificate.this[0].domain_validation_options : {
      name  = o.resource_record_name
      type  = o.resource_record_type
      value = o.resource_record_value
    }
  ] : []
}

output "waf_acl_arn" {
  value = local.waf_acl_arn
}

output "secrets_delivery" {
  value = local.secrets_delivery
}

output "public_url" {
  value = local.public_url
}

output "mcp_url" {
  value = "${local.public_url}/mcp"
}

output "namespace" {
  value = var.namespace
}

output "release_name" {
  value = helm_release.satchel.name
}

output "alb_hostname_command" {
  description = "Prints the ALB's DNS name once the controller has provisioned it; point the domain (CNAME or Route 53 alias) at it."
  value       = "kubectl -n ${var.namespace} get ingress ${local.release_fullname} -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'"
}
