output "service_name" {
  value = var.deploy_service ? google_cloud_run_v2_service.this[0].name : null
}

output "service_url" {
  description = "The run.app URL."
  value       = local.default_url
}

output "public_url" {
  description = "The URL configured as SATCHEL_PUBLIC_URL."
  value       = local.public_url
}

output "mcp_url" {
  value = "${local.public_url}/mcp"
}

output "job_names" {
  value = { for k, j in google_cloud_run_v2_job.this : k => j.name }
}

output "lb_ip_address" {
  description = "Create a DNS A record for the domain pointing here."
  value       = var.enable_load_balancer ? google_compute_global_address.lb[0].address : null
}

output "migrate_command" {
  value = "gcloud run jobs execute ${var.name}-migrate --region ${var.region} --project ${var.project_id} --wait"
}
