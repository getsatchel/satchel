# satchel on EKS against the floci AWS emulator (docs/deploy/floci.md). Applies the real
# aws-foundation and eks modules: VPC, KMS, RDS (a real pgvector Postgres), S3, Secrets Manager,
# IAM/IRSA roles, ACM, WAF and the EKS control plane, all emulated. floci's EKS has no
# Kubernetes behind it, so the Helm release installs on the k3s container from
# deploy/floci-aws/docker-compose.yml. NOT for real AWS.
#
#   deploy/floci-aws/up.sh && deploy/floci-aws/k3s-load-image.sh satchel:floci
#   terraform apply -var floci_host=$(deploy/floci-aws/floci-ip.sh)
terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
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
  # Endpoint the pods in k3s use for S3 (floci's address on the compose network).
  pod_endpoint = "http://${var.floci_host}:4566"
}

provider "aws" {
  region                      = var.region
  access_key                  = "test"
  secret_key                  = "test"
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_requesting_account_id  = true
  s3_use_path_style           = true

  endpoints {
    acm            = local.e
    ec2            = local.e
    eks            = local.e
    iam            = local.e
    kms            = local.e
    logs           = local.e
    rds            = local.e
    s3             = local.e
    secretsmanager = local.e
    sts            = local.e
    wafv2          = local.e
  }
}

# The k3s stand-in for the EKS data plane.
provider "kubernetes" {
  config_path = var.kubeconfig
}

provider "helm" {
  kubernetes {
    config_path = var.kubeconfig
  }
}

module "foundation" {
  source = "../../modules/aws-foundation"

  name         = var.name
  cluster_name = var.name
  az_count     = 2

  # Emulator gaps: floci's RDS proxy speaks plain TCP, and it reports the proxy port (7001)
  # instead of 5432, so every plan shows a port change on the instance. Pinning the URL host
  # keeps the generated secrets stable across applies.
  database_sslmode              = "disable"
  database_host_override        = "${var.floci_host}:${var.database_proxy_port}"
  database_deletion_protection  = false
  database_performance_insights = false

  oidc_client_secret = var.oidc_client_secret

  audit_bucket_force_destroy  = true
  secret_recovery_window_days = 0
}

module "eks" {
  source = "../../modules/eks"

  name = var.name

  vpc_id                     = module.foundation.vpc_id
  vpc_cidr                   = module.foundation.vpc_cidr
  private_subnet_ids         = module.foundation.private_subnet_ids
  kms_key_arn                = module.foundation.kms_key_arn
  database_security_group_id = module.foundation.database_security_group_id
  runtime_policy_arn         = module.foundation.runtime_policy_arn
  migrate_policy_arn         = module.foundation.migrate_policy_arn
  results_bucket             = module.foundation.results_bucket
  secret_names               = module.foundation.secret_names
  database_urls              = module.foundation.database_url_secrets
  database_passwords         = module.foundation.database_passwords
  encryption_key             = module.foundation.encryption_key

  cluster_deletion_protection = false
  node_min_size               = 1
  node_desired_size           = 1

  # Emulator gaps: no real cluster to run the AWS Load Balancer Controller or the Secrets Store
  # CSI driver (they need IMDS/IRSA against real AWS), so Terraform writes Kubernetes Secrets.
  install_cluster_addons = false
  secrets_delivery       = "kubernetes"

  domain             = var.domain
  image_tag          = var.image_tag
  oidc_issuer        = var.oidc_issuer
  oidc_client_id     = var.oidc_client_id
  oidc_client_secret = var.oidc_client_secret
  replicas           = 1
  first_workspace    = var.first_workspace

  # Chart overrides for k3s (top-level keys replace the module's generated ones):
  extra_values = {
    # The image was imported into k3s's containerd (deploy/floci-aws/k3s-load-image.sh).
    image = { repository = var.image_repository, tag = var.image_tag, pullPolicy = "Never" }
    # No ALB controller on k3s, no HPA metrics.
    exposure    = { type = "none", host = var.domain, timeoutSec = 3600 }
    autoscaling = { enabled = false }
    # k3s has no AWS NetworkPolicy semantics worth testing here.
    networkPolicy = { enabled = false }
    # Not baked into the test image.
    embedding = { provider = "hash", extra = {} }
    # No IRSA on k3s: boto3 gets the emulator's static test credentials and endpoint instead.
    extraEnv = {
      AWS_ENDPOINT_URL      = local.pod_endpoint
      AWS_ACCESS_KEY_ID     = "test"
      AWS_SECRET_ACCESS_KEY = "test"
    }
  }
}
