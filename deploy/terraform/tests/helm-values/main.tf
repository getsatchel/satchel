# Renders the GKE module's Helm values with sample inputs (no cloud access needed).
# Used by scripts/check-helm-values.sh.
terraform {
  required_version = ">= 1.6"
}

module "values" {
  source = "../../modules/helm-values"

  image_repository          = "ghcr.io/getsatchel/satchel"
  image_tag                 = "0.1.0"
  public_url                = "https://satchel.example.com"
  domain                    = "satchel.example.com"
  existing_secret_name      = "satchel-secrets"
  bootstrap_enabled         = true
  bootstrap_secret_name     = "satchel-bootstrap"
  service_account_name      = "satchel"
  gcp_service_account_email = "satchel-runtime@my-project.iam.gserviceaccount.com"
  instance_connection_name  = "my-project:us-central1:satchel-ab12"
  results_bucket            = "my-project-satchel-results"
  oidc_issuer               = "https://accounts.example.com"
  oidc_client_id            = "satchel"
  chat_provider             = "anthropic"
  chat_model                = "claude-sonnet-5-5"
  session_retention_days    = 90
  replicas                  = 2
  certificate_map           = "satchel-certmap"
  static_ip_name            = "satchel-gateway"
  first_workspace = {
    slug        = "acme"
    name        = "Acme"
    admin_email = "admin@example.com"
  }
}

output "values_yaml" {
  value = module.values.values_yaml
}
