variable "name" {
  description = "Name prefix (cluster, roles, Helm release)."
  type        = string
  default     = "satchel"
}

variable "tags" {
  type    = map(string)
  default = {}
}

# --- From aws-foundation -----------------------------------------------------

variable "vpc_id" {
  type = string
}

variable "vpc_cidr" {
  description = "VPC CIDR; when set, the NetworkPolicy admits the ALB (ip targets) from it. Null disables the NetworkPolicy."
  type        = string
  default     = null
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "kms_key_arn" {
  description = "Key for Kubernetes Secrets envelope encryption (the foundation key)."
  type        = string
}

variable "database_security_group_id" {
  description = "RDS security group; the module allows 5432 from the cluster security group."
  type        = string
}

variable "runtime_policy_arn" {
  type = string
}

variable "migrate_policy_arn" {
  type = string
}

variable "results_bucket" {
  type = string
}

variable "secret_names" {
  type = map(string)
}

variable "database_urls" {
  description = "Only used when secrets are delivered as Kubernetes Secrets (Fargate, or secrets_delivery = kubernetes)."
  type        = map(string)
  sensitive   = true
}

variable "database_passwords" {
  type      = map(string)
  sensitive = true
}

# --- Cluster -----------------------------------------------------------------

variable "create_cluster" {
  description = "Create the EKS cluster. false installs into existing_cluster_name (bring your own cluster, with its OIDC provider already registered in IAM)."
  type        = bool
  default     = true
}

variable "existing_cluster_name" {
  type    = string
  default = null
}

variable "kubernetes_version" {
  type    = string
  default = "1.33"
}

variable "endpoint_public_access" {
  description = "Expose the Kubernetes API endpoint publicly (restricted to public_access_cidrs). false = private endpoint only (run Terraform from inside the VPC)."
  type        = bool
  default     = true
}

variable "public_access_cidrs" {
  type    = list(string)
  default = ["0.0.0.0/0"]
}

variable "cluster_admin_principal_arns" {
  description = "IAM roles/users granted cluster admin through EKS access entries (in addition to the identity running Terraform)."
  type        = list(string)
  default     = []
}

variable "cluster_deletion_protection" {
  description = "EKS deletion protection on the cluster."
  type        = bool
  default     = true
}

variable "compute" {
  description = "managed_nodes: an EKS managed node group (Amazon Linux 2023, private subnets). fargate: Fargate profiles for the satchel and kube-system namespaces; no nodes to manage."
  type        = string
  default     = "managed_nodes"

  validation {
    condition     = contains(["managed_nodes", "fargate"], var.compute)
    error_message = "compute must be managed_nodes or fargate."
  }
}

variable "node_instance_types" {
  type    = list(string)
  default = ["m7i.large", "m6i.large"]
}

variable "node_capacity_type" {
  type    = string
  default = "ON_DEMAND"
}

variable "node_min_size" {
  type    = number
  default = 2
}

variable "node_max_size" {
  type    = number
  default = 6
}

variable "node_desired_size" {
  type    = number
  default = 2
}

variable "oidc_provider_arn" {
  description = "Existing IAM OIDC provider for the cluster. Null creates one (create_cluster) or looks it up (existing cluster)."
  type        = string
  default     = null
}

variable "install_cluster_addons" {
  description = "Install the AWS Load Balancer Controller and the Secrets Store CSI driver + AWS provider with Helm. false when the cluster already runs them (or for an emulator)."
  type        = bool
  default     = true
}

variable "load_balancer_controller_chart_version" {
  type    = string
  default = "3.6.2"
}

variable "secrets_provider_chart_version" {
  type    = string
  default = "3.1.4"
}

variable "secrets_delivery" {
  description = "csi (default on managed nodes): pods read Secrets Manager through the CSI driver, so a put-secret-value takes effect on the next pod start. kubernetes: Terraform writes Kubernetes Secrets (forced on Fargate, where the CSI driver's DaemonSet cannot run)."
  type        = string
  default     = "csi"

  validation {
    condition     = contains(["csi", "kubernetes"], var.secrets_delivery)
    error_message = "secrets_delivery must be csi or kubernetes."
  }
}

# --- Edge --------------------------------------------------------------------

variable "domain" {
  type = string
}

variable "route53_zone_id" {
  description = "Public hosted zone for the domain. When set, the module validates the ACM certificate with DNS records there. Null: validate it yourself (or pass certificate_arn)."
  type        = string
  default     = null
}

variable "certificate_arn" {
  description = "Existing ACM certificate for the domain. Null requests one."
  type        = string
  default     = null
}

variable "alb_scheme" {
  type    = string
  default = "internet-facing"
}

variable "enable_waf" {
  description = "Create an AWS WAF web ACL (AWS managed common, known-bad-inputs and IP reputation rules, plus a per-IP rate limit) and attach it to the ALB."
  type        = bool
  default     = true
}

variable "waf_rate_limit" {
  description = "Requests per 5 minutes per client IP before WAF blocks it."
  type        = number
  default     = 3000
}

variable "waf_acl_arn" {
  description = "Existing WAF web ACL to attach instead of creating one."
  type        = string
  default     = null
}

# --- Release -----------------------------------------------------------------

variable "chart_path" {
  type    = string
  default = null
}

variable "namespace" {
  type    = string
  default = "satchel"
}

variable "image_repository" {
  type    = string
  default = "ghcr.io/getsatchel/satchel"
}

variable "image_tag" {
  type = string
}

variable "oidc_issuer" {
  type = string
}

variable "oidc_client_id" {
  type = string
}

variable "oidc_client_secret" {
  description = "Only used for Kubernetes-Secret delivery; with CSI the value lives in Secrets Manager."
  type        = string
  default     = null
  sensitive   = true
}

variable "encryption_key" {
  description = "SATCHEL_ENCRYPTION_KEY: the foundation module's encryption_key output."
  type        = string
  sensitive   = true
  default     = null
}

variable "chat_api_key" {
  type      = string
  default   = null
  sensitive = true
}

variable "chat_provider" {
  type    = string
  default = null
}

variable "chat_model" {
  type    = string
  default = null
}

variable "replicas" {
  type    = number
  default = 2
}

variable "bootstrap_enabled" {
  type    = bool
  default = true
}

variable "first_workspace" {
  type = object({
    slug        = string
    name        = string
    admin_email = string
  })
  default = null
}

variable "extra_values" {
  type    = any
  default = {}
}

variable "helm_timeout" {
  type    = number
  default = 900
}

variable "session_retention_days" {
  description = "SATCHEL_SESSION_RETENTION_DAYS: delete agent sessions older than this many days (7-3650; sessions with feedback are kept). Null keeps them forever. Workspace admins can override it in Settings -> Limits."
  type        = number
  default     = null
}
