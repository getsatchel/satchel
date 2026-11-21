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

variable "existing_secret_name" {
  description = "Kubernetes Secret with SATCHEL_DATABASE_URL, SATCHEL_ADMIN_DATABASE_URL, SATCHEL_MIGRATION_DATABASE_URL, SATCHEL_OIDC_CLIENT_SECRET (and SATCHEL_CHAT_API_KEY)."
  type        = string
}

variable "bootstrap_enabled" {
  type    = bool
  default = true
}

variable "bootstrap_secret_name" {
  description = "Kubernetes Secret with SATCHEL_BOOTSTRAP_DATABASE_URL, SATCHEL_APP_DB_PASSWORD, SATCHEL_WORKER_DB_PASSWORD."
  type        = string
}

variable "service_account_name" {
  type = string
}

variable "gcp_service_account_email" {
  type = string
}

variable "instance_connection_name" {
  type = string
}

variable "results_bucket" {
  type = string
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

variable "certificate_map" {
  description = "Certificate Manager certificate map name for the Gateway. Null for none."
  type        = string
  default     = null
}

variable "static_ip_name" {
  description = "Reserved global static IP name for the Gateway. Null to let GKE assign one."
  type        = string
  default     = null
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

variable "session_retention_days" {
  description = "SATCHEL_SESSION_RETENTION_DAYS: delete agent sessions older than this many days (7-3650; sessions with feedback are kept). Null keeps them forever. Workspace admins can override it in Settings -> Limits."
  type        = number
  default     = null
  validation {
    condition     = var.session_retention_days == null ? true : (var.session_retention_days >= 7 && var.session_retention_days <= 3650 && floor(var.session_retention_days) == var.session_retention_days)
    error_message = "session_retention_days must be a whole number of days between 7 and 3650, or null."
  }
}

variable "secret_ref_allow" {
  description = "SATCHEL_SECRET_REF_ALLOW: the secret manager references (aws-sm:/gcp-sm: patterns) the UI and API accept. Empty turns references off."
  type        = list(string)
  default     = []
}

variable "secrets_mode" {
  description = "SATCHEL_SECRETS: \"store\" keeps secrets typed into the UI encrypted in Postgres; \"references\" refuses every secret value, so Satchel stores no credentials."
  type        = string
  default     = "store"
  validation {
    condition     = contains(["store", "references"], var.secrets_mode)
    error_message = "secrets_mode must be \"store\" or \"references\"."
  }
}
