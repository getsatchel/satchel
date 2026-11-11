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

  # Remote state: copy backend.tf.example to backend.tf and fill it in.
}

provider "google" {
  project = var.project_id
  region  = var.region
}

module "foundation" {
  source = "../../modules/foundation"

  project_id = var.project_id
  region     = var.region
  name       = var.name

  database_private_ip          = var.database_private_ip
  database_tier                = var.database_tier
  database_availability_type   = var.database_availability_type
  database_deletion_protection = var.database_deletion_protection

  oidc_client_secret  = var.oidc_client_secret
  enable_chat_api_key = var.enable_chat_api_key
  chat_api_key        = var.chat_api_key

  result_ttl_days      = var.result_ttl_days
  audit_retention_days = var.audit_retention_days
}

module "cloud_run" {
  source = "../../modules/cloud-run"

  project_id = var.project_id
  region     = var.region
  name       = var.name

  image                         = "${module.foundation.image_repository}:${var.image_tag}"
  service_account_email         = module.foundation.runtime_service_account_email
  migrate_service_account_email = module.foundation.migrate_service_account_email
  project_number                = module.foundation.project_number
  instance_connection_name      = module.foundation.instance_connection_name
  secret_ids                    = module.foundation.secret_ids
  results_bucket                = module.foundation.results_bucket
  audit_bucket                  = module.foundation.audit_bucket

  direct_vpc_egress = var.database_private_ip
  network_name      = module.foundation.network_name
  subnetwork_name   = module.foundation.subnetwork_name

  oidc_issuer            = var.oidc_issuer
  oidc_client_id         = var.oidc_client_id
  public_url             = var.public_url
  enable_chat_api_key    = var.enable_chat_api_key
  chat_provider          = var.chat_provider
  chat_model             = var.chat_model
  session_retention_days = var.session_retention_days

  deploy_service          = var.deploy_service
  min_instances           = var.min_instances
  max_instances           = var.max_instances
  run_migrations_on_apply = var.run_migrations_on_apply

  enable_load_balancer = var.domain != null
  domain               = var.domain
  enable_cloud_armor   = var.enable_cloud_armor
  enable_iap           = var.enable_iap
}
