output "url" {
  value = module.gke.public_url
}

output "mcp_url" {
  value = module.gke.mcp_url
}

output "gateway_address" {
  description = "Point the domain's A record at this address (it is reserved before the chart installs)."
  value       = module.gke.gateway_address
}

output "get_credentials_command" {
  value = module.gke.get_credentials_command
}

output "oidc_redirect_uri_hint" {
  value = "${module.gke.public_url}/auth/callback"
}

output "connect_claude_code" {
  value = "claude mcp add --transport http satchel ${module.gke.mcp_url}"
}

output "connect_claude_desktop" {
  value = "Settings > Connectors > Add custom connector > URL: ${module.gke.mcp_url}"
}

output "migrate_note" {
  value = "The chart runs migrations as a pre-install/pre-upgrade hook Job; see docs/deploy/gke.md."
}
