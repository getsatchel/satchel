# satchel on GKE against the floci-gcp emulator (docs/deploy/floci.md). floci runs the cluster as a
# real k3s in Docker, so the Helm release really installs: migration hook (bootstrap + upgrade),
# first-workspace Job, web Deployment, worker CronJob. NOT for real GCP.
#
# Two-step apply (the Kubernetes and Helm providers are configured from the cluster):
#   terraform apply -target=module.foundation -target=terraform_data.cloudsql_shim \
#     -target=module.gke.google_container_cluster.this
#   deploy/floci/k3s-load-image.sh satchel satchel:floci     # the image is local, not in a registry
#   terraform apply
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
}

locals {
  e = trimsuffix(var.floci_endpoint, "/")
  # Emulator gap (provider): GKE must use floci's host routing (container.<host>), not a path.
  container_endpoint = "http://container.localhost:${var.floci_port}/"
}

provider "google" {
  project      = var.project_id
  region       = var.region
  access_token = "floci-dummy-token"

  user_project_override = false

  service_usage_custom_endpoint    = "${local.e}/"
  resource_manager_custom_endpoint = "${local.e}/"
  cloud_billing_custom_endpoint    = "${local.e}/"
  iam_custom_endpoint              = "${local.e}/"
  compute_custom_endpoint          = "${local.e}/compute/v1/"
  sql_custom_endpoint              = "${local.e}/"
  secret_manager_custom_endpoint   = "${local.e}/v1/"
  storage_custom_endpoint          = "${local.e}/storage/v1/"
  container_custom_endpoint        = local.container_endpoint
}

data "google_client_config" "default" {}

# k3s accepts any bearer token (floci's token webhook maps it to cluster-admin).
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

  # Emulator gaps, as in examples/floci-cloud-run: no Private Service Access, no Cloud SQL Auth
  # Proxy (so TCP straight to the instance IP), no Artifact Registry, database created by the shim.
  database_private_ip           = false
  create_private_service_access = false
  database_deletion_protection  = false
  database_url_style            = "tcp"
  database_tcp_host             = "instance"
  create_database               = false

  oidc_client_secret = var.oidc_client_secret

  create_ghcr_remote_repository = false
  image_repository_override     = var.image_repository

  audit_bucket_force_destroy = true
}

resource "terraform_data" "cloudsql_shim" {
  triggers_replace = [module.foundation.instance_name]

  provisioner "local-exec" {
    command = "${path.module}/../../../floci/cloudsql-shim.sh ${module.foundation.instance_name}"
    environment = {
      FLOCI_ENDPOINT = local.e
      FLOCI_PROJECT  = var.project_id
    }
  }
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

  domain             = var.domain
  oidc_issuer        = var.oidc_issuer
  oidc_client_id     = var.oidc_client_id
  oidc_client_secret = var.oidc_client_secret
  replicas           = 1
  first_workspace    = var.first_workspace

  cluster_deletion_protection = false
  # Emulator gaps: no Cloud Router / NAT, no Certificate Manager.
  create_nat                 = false
  enable_certificate_manager = false

  # Chart overrides for k3s (top-level keys replace the module's generated ones):
  extra_values = {
    # The image was imported into k3s's containerd (deploy/floci/k3s-load-image.sh).
    image = { repository = var.image_repository, tag = var.image_tag, pullPolicy = "Never" }
    # Emulator gap: the Auth Proxy needs the real Cloud SQL connect API; the DB URLs are TCP.
    cloudSqlProxy = { enabled = false }
    # Emulator gap: k3s has neither the GKE Gateway classes nor GKE's policy CRDs.
    exposure    = { type = "none", host = var.domain, timeoutSec = 3600 }
    gke         = { enabled = false }
    autoscaling = { enabled = false }
    # Not baked into the test image.
    embedding = { provider = "hash", extra = {} }
    # google-cloud-storage sends result reads/writes to the emulator.
    extraEnv = var.storage_emulator_host == "" ? {} : { STORAGE_EMULATOR_HOST = var.storage_emulator_host }
  }

  depends_on = [terraform_data.cloudsql_shim]
}
