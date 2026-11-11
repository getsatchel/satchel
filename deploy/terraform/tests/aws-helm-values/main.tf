# Renders the EKS module's Helm values with sample inputs (no cloud access needed), once per
# secrets delivery mode. Used by scripts/check-helm-values.sh.
terraform {
  required_version = ">= 1.6"
}

locals {
  common = {
    image_repository = "ghcr.io/getsatchel/satchel"
    image_tag        = "0.1.0"
    public_url       = "https://satchel.example.com"
    domain           = "satchel.example.com"
    region           = "eu-west-2"
    results_bucket   = "123456789012-satchel-results"
    runtime_role_arn = "arn:aws:iam::123456789012:role/satchel-runtime"
    migrate_role_arn = "arn:aws:iam::123456789012:role/satchel-migrate"
    oidc_issuer      = "https://accounts.example.com"
    oidc_client_id   = "satchel"
  }
  secret_names = {
    for k in ["owner_password", "app_password", "worker_password", "database_url", "admin_database_url", "migration_database_url", "oidc_client_secret", "chat_api_key", "encryption_key"] :
    k => "satchel/${replace(k, "_", "-")}"
  }
}

module "csi" {
  source = "../../modules/aws-helm-values"

  image_repository       = local.common.image_repository
  image_tag              = local.common.image_tag
  public_url             = local.common.public_url
  domain                 = local.common.domain
  region                 = local.common.region
  results_bucket         = local.common.results_bucket
  runtime_role_arn       = local.common.runtime_role_arn
  migrate_role_arn       = local.common.migrate_role_arn
  oidc_issuer            = local.common.oidc_issuer
  oidc_client_id         = local.common.oidc_client_id
  secrets_delivery       = "csi"
  secret_names           = local.secret_names
  chat_provider          = "anthropic"
  chat_model             = "claude-sonnet-5-5"
  session_retention_days = 90
  certificate_arn        = "arn:aws:acm:eu-west-2:123456789012:certificate/0000-1111"
  waf_acl_arn            = "arn:aws:wafv2:eu-west-2:123456789012:regional/webacl/satchel/abcd"
  network_policy_cidrs   = ["10.40.0.0/16"]
  first_workspace = {
    slug        = "acme"
    name        = "Acme"
    admin_email = "admin@example.com"
  }
}

module "kubernetes" {
  source = "../../modules/aws-helm-values"

  image_repository      = local.common.image_repository
  image_tag             = local.common.image_tag
  public_url            = local.common.public_url
  domain                = local.common.domain
  region                = local.common.region
  results_bucket        = local.common.results_bucket
  runtime_role_arn      = local.common.runtime_role_arn
  migrate_role_arn      = local.common.migrate_role_arn
  oidc_issuer           = local.common.oidc_issuer
  oidc_client_id        = local.common.oidc_client_id
  secrets_delivery      = "kubernetes"
  existing_secret_name  = "satchel-secrets"
  bootstrap_secret_name = "satchel-bootstrap"
  worker_mode           = "deployment"
}

output "values_yaml_csi" {
  value = module.csi.values_yaml
}

output "values_yaml_kubernetes" {
  value = module.kubernetes.values_yaml
}
