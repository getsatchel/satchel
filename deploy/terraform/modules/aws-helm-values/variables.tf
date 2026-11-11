variable "image_repository" {
  type = string
}

variable "image_tag" {
  type = string
}

variable "public_url" {
  type = string
}

variable "domain" {
  type = string
}

variable "region" {
  description = "AWS region (AWS_REGION in the pods, Secrets Manager region)."
  type        = string
}

variable "results_bucket" {
  type = string
}

variable "runtime_role_arn" {
  description = "IRSA role for web, worker and the first-workspace Job."
  type        = string
}

variable "migrate_role_arn" {
  description = "IRSA role for the migrate Job (owner-level secrets)."
  type        = string
}

variable "secrets_delivery" {
  description = "csi: the Secrets Store CSI driver + AWS provider reads Secrets Manager at pod start (secret_names). kubernetes: Terraform-managed Kubernetes Secrets (existing_secret_name, bootstrap_secret_name), for Fargate, where the CSI driver cannot run."
  type        = string
  default     = "csi"

  validation {
    condition     = contains(["csi", "kubernetes"], var.secrets_delivery)
    error_message = "secrets_delivery must be csi or kubernetes."
  }
}

variable "secret_names" {
  description = "Secrets Manager names keyed like aws-foundation's secret_names (database_url, oidc_client_secret, ...). Used when secrets_delivery is csi."
  type        = map(string)
  default     = {}
}

variable "existing_secret_name" {
  description = "Kubernetes Secret with the runtime keys. Used when secrets_delivery is kubernetes."
  type        = string
  default     = null
}

variable "bootstrap_secret_name" {
  description = "Kubernetes Secret with the bootstrap keys. Used when secrets_delivery is kubernetes."
  type        = string
  default     = null
}

variable "bootstrap_enabled" {
  type    = bool
  default = true
}

variable "oidc_issuer" {
  type = string
}

variable "oidc_client_id" {
  type = string
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

variable "worker_mode" {
  type    = string
  default = "cronjob"
}

variable "exposure" {
  description = "ingress: an ALB through the AWS Load Balancer Controller. none: no Ingress (port-forward or your own)."
  type        = string
  default     = "ingress"
}

variable "alb_scheme" {
  type    = string
  default = "internet-facing"
}

variable "certificate_arn" {
  type    = string
  default = null
}

variable "waf_acl_arn" {
  type    = string
  default = null
}

variable "network_policy_cidrs" {
  description = "Source CIDRs allowed to the web port when the NetworkPolicy is on (the VPC CIDR, for the ALB). Empty disables the NetworkPolicy."
  type        = list(string)
  default     = []
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
  description = "Deep-merged last (top-level keys replace)."
  type        = any
  default     = {}
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
