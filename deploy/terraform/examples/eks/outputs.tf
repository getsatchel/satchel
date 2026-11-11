output "url" {
  value = module.eks.public_url
}

output "mcp_url" {
  value = module.eks.mcp_url
}

output "update_kubeconfig_command" {
  value = module.eks.update_kubeconfig_command
}

output "alb_hostname_command" {
  description = "Run once the ALB is up; point the domain at the printed hostname (CNAME or Route 53 alias)."
  value       = module.eks.alb_hostname_command
}

output "certificate_validation_records" {
  value = module.eks.certificate_validation_records
}

output "oidc_secret_command" {
  description = "Store the IdP client secret (when oidc_client_secret was not set)."
  value       = "aws secretsmanager put-secret-value --region ${module.foundation.region} --secret-id ${module.foundation.secret_names["oidc_client_secret"]} --secret-string \"$OIDC_CLIENT_SECRET\""
}

output "oidc_redirect_uri_hint" {
  value = "${module.eks.public_url}/auth/callback"
}

output "connect_claude_code" {
  value = "claude mcp add --transport http satchel ${module.eks.mcp_url}"
}

output "results_bucket" {
  value = module.foundation.results_bucket
}

output "audit_bucket" {
  value = module.foundation.audit_bucket
}

output "database_endpoint" {
  value = module.foundation.database_endpoint
}

output "secrets_delivery" {
  value = module.eks.secrets_delivery
}
