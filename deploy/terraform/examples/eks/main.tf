# satchel on Amazon EKS: VPC (or yours), RDS for PostgreSQL 17 + pgvector, S3, Secrets Manager,
# KMS, IRSA, the AWS Load Balancer Controller with an ACM certificate and AWS WAF, and the Helm
# release. See docs/deploy/eks.md.
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

  # Remote state: copy backend.tf.example to backend.tf and fill it in.
}

provider "aws" {
  region = var.region

  default_tags {
    tags = merge({ app = "satchel" }, var.tags)
  }
}

# The Kubernetes and Helm providers are configured from the cluster the same apply creates. On
# the very first run, create the cluster first (docs/deploy/eks.md step 3):
#   terraform apply -target=module.eks.aws_eks_cluster.this
data "aws_eks_cluster_auth" "this" {
  name = module.eks.cluster_name
}

provider "kubernetes" {
  host                   = module.eks.cluster_endpoint
  token                  = data.aws_eks_cluster_auth.this.token
  cluster_ca_certificate = base64decode(module.eks.cluster_ca_certificate)
}

provider "helm" {
  kubernetes {
    host                   = module.eks.cluster_endpoint
    token                  = data.aws_eks_cluster_auth.this.token
    cluster_ca_certificate = base64decode(module.eks.cluster_ca_certificate)
  }
}

module "foundation" {
  source = "../../modules/aws-foundation"

  name         = var.name
  cluster_name = var.name

  create_vpc         = var.vpc_id == null
  vpc_id             = var.vpc_id
  private_subnet_ids = var.private_subnet_ids
  public_subnet_ids  = var.public_subnet_ids
  single_nat_gateway = var.single_nat_gateway

  database_instance_class      = var.database_instance_class
  database_multi_az            = var.database_multi_az
  database_deletion_protection = var.database_deletion_protection

  oidc_client_secret  = var.oidc_client_secret
  enable_chat_api_key = var.chat_api_key != null
  chat_api_key        = var.chat_api_key

  result_ttl_days      = var.result_ttl_days
  audit_retention_days = var.audit_retention_days
}

module "eks" {
  source = "../../modules/eks"

  name = var.name

  vpc_id                     = module.foundation.vpc_id
  vpc_cidr                   = module.foundation.vpc_cidr != null ? module.foundation.vpc_cidr : var.vpc_cidr_for_network_policy
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

  create_cluster               = var.existing_cluster_name == null
  existing_cluster_name        = var.existing_cluster_name
  kubernetes_version           = var.kubernetes_version
  compute                      = var.compute
  node_instance_types          = var.node_instance_types
  public_access_cidrs          = var.cluster_public_access_cidrs
  cluster_admin_principal_arns = var.cluster_admin_principal_arns
  cluster_deletion_protection  = var.cluster_deletion_protection

  domain          = var.domain
  route53_zone_id = var.route53_zone_id
  certificate_arn = var.certificate_arn
  enable_waf      = var.enable_waf

  image_tag              = var.image_tag
  oidc_issuer            = var.oidc_issuer
  oidc_client_id         = var.oidc_client_id
  oidc_client_secret     = var.oidc_client_secret
  chat_api_key           = var.chat_api_key
  chat_provider          = var.chat_provider
  chat_model             = var.chat_model
  session_retention_days = var.session_retention_days
  replicas               = var.replicas
  bootstrap_enabled      = var.bootstrap_enabled
  first_workspace        = var.first_workspace
  extra_values           = var.helm_extra_values
}
