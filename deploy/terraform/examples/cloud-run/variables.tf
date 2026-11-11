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
  description = "satchel image tag, for example 0.1.0. Pin a version."
  type        = string
}

variable "oidc_issuer" {
  type = string
}

variable "oidc_client_id" {
  type = string
}

variable "oidc_client_secret" {
  description = "Leave null to create an empty secret and add the value with gcloud (keeps it out of Terraform state)."
  type        = string
  default     = null
  sensitive   = true
}

variable "enable_chat_api_key" {
  type    = bool
  default = false
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

variable "domain" {
  description = "Set to serve satchel on your own domain through an external HTTPS load balancer. Null uses the run.app URL."
  type        = string
  default     = null
}

variable "public_url" {
  description = "Override SATCHEL_PUBLIC_URL, for example when a domain mapping is managed elsewhere."
  type        = string
  default     = null
}

variable "enable_cloud_armor" {
  type    = bool
  default = false
}

variable "enable_iap" {
  type    = bool
  default = false
}

variable "database_private_ip" {
  type    = bool
  default = true
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

variable "result_ttl_days" {
  type    = number
  default = 7
}

variable "audit_retention_days" {
  type    = number
  default = null
}

variable "deploy_service" {
  description = "false on the first apply when you will run migrations by hand."
  type        = bool
  default     = true
}

variable "min_instances" {
  type    = number
  default = 1
}

variable "max_instances" {
  type    = number
  default = 5
}

variable "run_migrations_on_apply" {
  type    = bool
  default = false
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
