# satchel on Cloud Run against the floci-gcp emulator (docs/deploy/floci.md). NOT for real GCP:
# the emulator profile puts secret values into the service spec.
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
  }
}

locals {
  e = trimsuffix(var.floci_endpoint, "/")

  # floci's Cloud Run front door: <service>-<first 12 hex of sha256(project)>.<region>.run.<suffix>
  public_url = "http://${var.name}-${substr(sha256(var.project_id), 0, 12)}.${var.region}.run.localhost.floci.io:${var.floci_port}"
}

# Every API the modules call, pointed at the emulator. floci does not check credentials.
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
  cloud_run_v2_custom_endpoint     = "${local.e}/v2/"
  cloud_scheduler_custom_endpoint  = "${local.e}/v1/"
}

module "foundation" {
  source = "../../modules/foundation"

  project_id = var.project_id
  region     = var.region
  name       = var.name

  # Emulator gap: no Service Networking (Private Service Access). Public IP, no authorized
  # networks, which is also the production fallback; the emulator reports the container IP.
  database_private_ip           = false
  create_private_service_access = false
  database_deletion_protection  = false
  # Emulator gap: no Cloud SQL connector, so TCP to the instance IP instead of the unix socket.
  database_url_style = "tcp"
  database_tcp_host  = "instance"
  # Emulator gap (provider): hashicorp/google 6.x only strips API versions from https custom
  # endpoints, so on plain http one sql_custom_endpoint cannot serve both the instance and the
  # database resource. cloudsql-shim.sh creates the database through the Admin API instead.
  create_database = false

  oidc_client_secret = var.oidc_client_secret

  # Emulator gap: no Artifact Registry; floci runs local images through the host Docker daemon.
  create_ghcr_remote_repository = false
  image_repository_override     = var.image_repository

  audit_bucket_force_destroy = true
}

# Emulator gap: floci's Cloud SQL users are plain roles; give the owner the cloudsqlsuperuser
# setup that real Cloud SQL gives every API-created user (deploy/floci/cloudsql-shim.sh).
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

module "cloud_run" {
  source = "../../modules/cloud-run"

  project_id = var.project_id
  region     = var.region
  name       = var.name

  # Emulator gaps: no secret references, no Cloud SQL volume, no startup probe.
  runtime_profile        = "emulator"
  emulator_secret_values = module.foundation.secret_values

  image                         = "${module.foundation.image_repository}:${var.image_tag}"
  service_account_email         = module.foundation.runtime_service_account_email
  migrate_service_account_email = module.foundation.migrate_service_account_email
  project_number                = module.foundation.project_number
  instance_connection_name      = module.foundation.instance_connection_name
  secret_ids                    = module.foundation.secret_ids
  results_bucket                = module.foundation.results_bucket
  audit_bucket                  = module.foundation.audit_bucket

  # Public IP database: no Direct VPC egress.
  direct_vpc_egress = false

  oidc_issuer    = var.oidc_issuer
  oidc_client_id = var.oidc_client_id
  public_url     = local.public_url
  # The fastembed model is not baked into the test image and there is no egress guarantee.
  embedding_provider = "hash"
  extra_env = {
    # floci's front door is plain http; SATCHEL_ENV stays production.
    SATCHEL_ALLOW_INSECURE_PUBLIC_URL = "1"
    # google-cloud-storage sends GCS calls (results) to the emulator.
    STORAGE_EMULATOR_HOST = var.storage_emulator_host
  }

  # Emulator gap: floci returns Cloud Scheduler jobs without the default User-Agent header and
  # the provider panics reading an empty header map. An explicit header avoids that.
  scheduler_http_headers = { "Content-Type" = "application/json" }

  deploy_service = var.deploy_service
  min_instances  = 1
  max_instances  = 1
  cpu            = "1"
  memory         = "1Gi"

  # No load balancer, certificates, Cloud Armor or IAP: floci only stores LB config.
  enable_load_balancer = false

  depends_on = [terraform_data.cloudsql_shim]
}
