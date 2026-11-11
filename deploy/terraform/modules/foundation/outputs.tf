output "project_id" {
  value = var.project_id
}

output "project_number" {
  value = data.google_project.this.number
}

output "region" {
  value = var.region
}

output "instance_name" {
  value = google_sql_database_instance.this.name
}

output "instance_connection_name" {
  description = "PROJECT:REGION:INSTANCE, for the Cloud SQL connector and Auth Proxy."
  value       = google_sql_database_instance.this.connection_name
}

output "database_private_ip" {
  value = google_sql_database_instance.this.private_ip_address
}

output "secret_ids" {
  description = "Secret Manager secret ids by role."
  value       = local.secret_ids
}

output "database_url_secrets" {
  description = "Sensitive DB URL values (used by the GKE module to build a Kubernetes Secret)."
  value       = local.database_urls
  sensitive   = true
}

output "encryption_key" {
  description = "SATCHEL_ENCRYPTION_KEY (for Kubernetes Secrets when the CSI driver is not used)."
  value       = random_id.encryption_key.hex
  sensitive   = true
}

output "database_passwords" {
  description = "Generated role passwords (bootstrap only)."
  value = {
    app    = random_password.app.result
    worker = random_password.worker.result
  }
  sensitive = true
}

output "runtime_service_account_email" {
  value = google_service_account.runtime.email
}

output "migrate_service_account_email" {
  description = "Identity for the migrate job only: it can read the owner-level secrets."
  value       = google_service_account.migrate.email
}

output "runtime_service_account_name" {
  value = google_service_account.runtime.name
}

output "results_bucket" {
  value = google_storage_bucket.results.name
}

output "audit_bucket" {
  value = google_storage_bucket.audit.name
}

output "image_repository" {
  description = "Image repository (no tag) the workloads should pull."
  value       = local.image_repository
}

output "network_id" {
  value = local.network_id
}

output "network_name" {
  value = local.network_name
}

output "subnetwork_name" {
  value = local.subnetwork_name
}

output "subnetwork_id" {
  value = local.subnetwork_id
}

output "secret_values" {
  description = "Secret values keyed like secret_ids (shell secrets only when a value was supplied). For emulators that cannot resolve Secret Manager references (cloud-run runtime_profile = \"emulator\")."
  value = merge(
    local.secret_values,
    { for k in local.shell_keys : k => local.shell_values[k] if local.shell_has_value[k] },
  )
  sensitive = true
}
