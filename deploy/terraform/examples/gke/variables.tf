variable "project_id" {
  type = string
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "name" {
  type    = string
  default = "satchel"
}

variable "image_tag" {
  type = string
}

variable "domain" {
  description = "Public hostname served by the Gateway, for example satchel.example.com."
  type        = string
}

variable "oidc_issuer" {
  type = string
}

variable "oidc_client_id" {
  type = string
}

variable "oidc_client_secret" {
  description = "Required on GKE: it is written to the Kubernetes Secret (and Secret Manager)."
  type        = string
  sensitive   = true
}

variable "chat_api_key" {
  type      = string
  default   = null
  sensitive = true
}

variable "replicas" {
  type    = number
  default = 2
}

variable "database_tier" {
  type    = string
  default = "db-custom-1-3840"
}

variable "database_availability_type" {
  type    = string
  default = "ZONAL"
}

variable "database_deletion_protection" {
  type    = bool
  default = true
}

variable "cluster_deletion_protection" {
  type    = bool
  default = true
}

variable "master_authorized_networks" {
  description = "Name => CIDR of networks allowed to reach the control plane."
  type        = map(string)
  default     = {}
}

variable "result_ttl_days" {
  type    = number
  default = 7
}

variable "audit_retention_days" {
  type    = number
  default = null
}

variable "helm_extra_values" {
  type    = any
  default = {}
}

variable "bootstrap_enabled" {
  description = "false after the first install, to stop mounting the owner credential."
  type        = bool
  default     = true
}

variable "chat_provider" {
  type    = string
  default = null
}

variable "chat_model" {
  type    = string
  default = null
}

variable "first_workspace" {
  type = object({
    slug        = string
    name        = string
    admin_email = string
  })
  default = null
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
