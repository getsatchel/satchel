terraform {
  required_version = ">= 1.6"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.17"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.36"
    }
  }

  # Remote state: copy backend.tf.example to backend.tf and fill it in.
}

provider "google" {
  project = var.project_id
  region  = var.region
}

data "google_client_config" "default" {}

# These providers are configured from the cluster the same apply creates. On the very first run,
# create the cluster first (docs/deploy/gke.md step 3):
#   terraform apply -target=module.gke.google_container_cluster.this
provider "kubernetes" {
  host                   = "https://${module.gke.cluster_endpoint}"
  token                  = data.google_client_config.default.access_token
  cluster_ca_certificate = base64decode(module.gke.cluster_ca_certificate)
}

provider "helm" {
  kubernetes {
    host                   = "https://${module.gke.cluster_endpoint}"
    token                  = data.google_client_config.default.access_token
    cluster_ca_certificate = base64decode(module.gke.cluster_ca_certificate)
  }
}

module "foundation" {
  source = "../../modules/foundation"

  project_id = var.project_id
  region     = var.region
  name       = var.name

  # GKE reaches Cloud SQL through the Auth Proxy sidecar on 127.0.0.1:5432.
  database_url_style           = "tcp"
  database_private_ip          = true
  database_tier                = var.database_tier
  database_availability_type   = var.database_availability_type
  database_deletion_protection = var.database_deletion_protection

  oidc_client_secret  = var.oidc_client_secret
  enable_chat_api_key = var.chat_api_key != null
  chat_api_key        = var.chat_api_key

  result_ttl_days      = var.result_ttl_days
  audit_retention_days = var.audit_retention_days

  # GKE pulls ghcr.io directly through Cloud NAT; no remote repository needed.
  create_ghcr_remote_repository = false
}

module "gke" {
  source = "../../modules/gke"

  project_id = var.project_id
  region     = var.region
  name       = var.name

  network_id                    = module.foundation.network_id
  subnetwork_id                 = module.foundation.subnetwork_id
  router_network_name           = module.foundation.network_name
  instance_connection_name      = module.foundation.instance_connection_name
  runtime_service_account_email = module.foundation.runtime_service_account_email
  runtime_service_account_name  = module.foundation.runtime_service_account_name
  results_bucket                = module.foundation.results_bucket

  image_repository   = module.foundation.image_repository
  image_tag          = var.image_tag
  database_urls      = module.foundation.database_url_secrets
  database_passwords = module.foundation.database_passwords
  encryption_key     = module.foundation.encryption_key

  domain                 = var.domain
  oidc_issuer            = var.oidc_issuer
  oidc_client_id         = var.oidc_client_id
  oidc_client_secret     = var.oidc_client_secret
  chat_api_key           = var.chat_api_key
  replicas               = var.replicas
  bootstrap_enabled      = var.bootstrap_enabled
  chat_provider          = var.chat_provider
  chat_model             = var.chat_model
  session_retention_days = var.session_retention_days
  first_workspace        = var.first_workspace

  cluster_deletion_protection = var.cluster_deletion_protection
  master_authorized_networks  = var.master_authorized_networks
  extra_values                = var.helm_extra_values
}
