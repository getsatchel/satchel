variable "region" {
  type    = string
  default = "eu-west-2"
}

variable "name" {
  type    = string
  default = "satchel"
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "image_tag" {
  type = string
}

variable "domain" {
  description = "Public hostname served by the ALB, for example satchel.example.com."
  type        = string
}

variable "route53_zone_id" {
  description = "Public hosted zone of the domain, for automatic ACM validation. Null: create the validation records yourself (output certificate_validation_records)."
  type        = string
  default     = null
}

variable "certificate_arn" {
  description = "Existing ACM certificate instead of requesting one."
  type        = string
  default     = null
}

variable "enable_waf" {
  type    = bool
  default = true
}

variable "oidc_issuer" {
  type = string
}

variable "oidc_client_id" {
  type = string
}

variable "oidc_client_secret" {
  description = "Optional. Null leaves the Secrets Manager secret empty: put the value with aws secretsmanager put-secret-value after apply (docs/deploy/eks.md). Required with compute = fargate."
  type        = string
  default     = null
  sensitive   = true
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
  description = "Run `satchel db bootstrap` in the migrate hook. Set false after the first install."
  type        = bool
  default     = true
}

variable "first_workspace" {
  type = object({
    slug        = string
    name        = string
    admin_email = string
  })
  default = null
}

# --- Network: leave vpc_id null to create one ---------------------------------

variable "vpc_id" {
  type    = string
  default = null
}

variable "private_subnet_ids" {
  type    = list(string)
  default = []
}

variable "public_subnet_ids" {
  type    = list(string)
  default = []
}

variable "vpc_cidr_for_network_policy" {
  description = "With an existing VPC: its CIDR, so the NetworkPolicy admits the ALB."
  type        = string
  default     = null
}

variable "single_nat_gateway" {
  type    = bool
  default = true
}

# --- Cluster ----------------------------------------------------------------------

variable "existing_cluster_name" {
  description = "Install into an existing EKS cluster (with its IAM OIDC provider) instead of creating one."
  type        = string
  default     = null
}

variable "kubernetes_version" {
  type    = string
  default = "1.33"
}

variable "compute" {
  description = "managed_nodes or fargate."
  type        = string
  default     = "managed_nodes"
}

variable "node_instance_types" {
  type    = list(string)
  default = ["m7i.large", "m6i.large"]
}

variable "cluster_public_access_cidrs" {
  description = "Who may reach the Kubernetes API endpoint. Lock it to your office or VPN."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "cluster_admin_principal_arns" {
  type    = list(string)
  default = []
}

variable "cluster_deletion_protection" {
  type    = bool
  default = true
}

# --- Data ---------------------------------------------------------------------------

variable "database_instance_class" {
  type    = string
  default = "db.t4g.medium"
}

variable "database_multi_az" {
  type    = bool
  default = false
}

variable "database_deletion_protection" {
  type    = bool
  default = true
}

variable "result_ttl_days" {
  type    = number
  default = 7
}

variable "audit_retention_days" {
  description = "S3 Object Lock retention for audit exports (GOVERNANCE mode). Null: versioning only."
  type        = number
  default     = null
}

variable "helm_extra_values" {
  type    = any
  default = {}
}

variable "session_retention_days" {
  description = "SATCHEL_SESSION_RETENTION_DAYS: delete agent sessions older than this many days (7-3650; sessions with feedback are kept). Null keeps them forever. Workspace admins can override it in Settings -> Limits."
  type        = number
  default     = null
  validation {
    condition     = var.session_retention_days == null ? true : (var.session_retention_days >= 7 && var.session_retention_days <= 3650 && floor(var.session_retention_days) == var.session_retention_days)
    error_message = "session_retention_days must be a whole number of days between 7 and 3650, or null."
  }
}
