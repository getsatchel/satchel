output "url" {
  description = "satchel web UI."
  value       = module.cloud_run.public_url
}

output "mcp_url" {
  description = "MCP endpoint (Streamable HTTP)."
  value       = module.cloud_run.mcp_url
}

output "oidc_redirect_uri_hint" {
  description = "Register this redirect URI on the OIDC client (confirm the exact path against the satchel docs for your version)."
  value       = "${module.cloud_run.public_url}/auth/callback"
}

output "lb_ip_address" {
  value = module.cloud_run.lb_ip_address
}

output "migrate_command" {
  value = module.cloud_run.migrate_command
}

output "oidc_secret_id" {
  description = "Add a version here when oidc_client_secret was left null."
  value       = module.foundation.secret_ids["oidc_client_secret"]
}

output "connect_claude_code" {
  value = "claude mcp add --transport http satchel ${module.cloud_run.mcp_url}"
}

output "connect_claude_desktop" {
  value = "Settings > Connectors > Add custom connector > URL: ${module.cloud_run.mcp_url}"
}

output "image" {
  value = "${module.foundation.image_repository}:${var.image_tag}"
}
