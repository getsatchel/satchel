output "cluster_name" {
  value = google_container_cluster.this.name
}

output "get_credentials_command" {
  value = "gcloud container clusters get-credentials ${google_container_cluster.this.name} --region ${var.region} --project ${var.project_id}"
}

output "gateway_address" {
  description = "Reserved global IP of the Gateway; point the domain's A record at it."
  value       = google_compute_global_address.gateway.address
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

output "cluster_endpoint" {
  value = google_container_cluster.this.endpoint
}

output "cluster_ca_certificate" {
  value = google_container_cluster.this.master_auth[0].cluster_ca_certificate
}
