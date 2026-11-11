variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

variable "name" {
  description = "Service name; jobs are <name>-migrate, <name>-worker and <name>-audit-export."
  type        = string
  default     = "satchel"
}

variable "labels" {
  type    = map(string)
  default = {}
}

# --- Inputs from the foundation module -------------------------------------

variable "image" {
  description = "Full image reference including tag, for example <foundation image_repository>:0.1.0. Pin a version tag: a floating tag does not roll a new revision."
  type        = string
}

variable "service_account_email" {
  description = "Runtime service account (foundation output)."
  type        = string
}

variable "migrate_service_account_email" {
  description = "Identity for the migrate job (foundation migrate_service_account_email); it can read the owner-level secrets the runtime identity cannot. Null runs the job as the runtime identity, which then lacks those secrets."
  type        = string
  default     = null
}

variable "project_number" {
  description = "Project number, used to compute the default run.app URL."
  type        = string
}

variable "instance_connection_name" {
  description = "Cloud SQL connection name PROJECT:REGION:INSTANCE."
  type        = string
}

variable "secret_ids" {
  description = "foundation secret_ids output."
  type        = map(string)
}

variable "results_bucket" {
  type = string
}

variable "audit_bucket" {
  type = string
}

variable "network_name" {
  description = "VPC for Direct VPC egress. Required when the database has a private IP."
  type        = string
  default     = null
}

variable "subnetwork_name" {
  type    = string
  default = null
}

variable "direct_vpc_egress" {
  description = "Attach the service and jobs to the VPC (Direct VPC egress, private ranges only). Required for a private-IP Cloud SQL instance."
  type        = bool
  default     = true
}

# --- Runtime profile -------------------------------------------------------

variable "runtime_profile" {
  description = "gcp (default): secrets from Secret Manager, Cloud SQL through the built-in connector socket, /readyz startup probe. emulator: for local emulators such as floci-gcp (docs/deploy/floci.md), which support none of those: plain env values from emulator_secret_values, TCP DB URLs (foundation database_url_style = \"tcp\"), no Cloud SQL volume, no startup probe. Never use emulator on real GCP: secret values land in the service spec."
  type        = string
  default     = "gcp"

  validation {
    condition     = contains(["gcp", "emulator"], var.runtime_profile)
    error_message = "runtime_profile must be gcp or emulator."
  }
}

variable "emulator_secret_values" {
  description = "runtime_profile = emulator only: secret values by secret key (the keys of secret_ids: database_url, admin_database_url, migration_database_url, oidc_client_secret, app_password, worker_password, chat_api_key, encryption_key)."
  type        = map(string)
  default     = {}
  sensitive   = true
}

# --- Application config ----------------------------------------------------

variable "oidc_issuer" {
  description = "OIDC issuer URL, for example https://accounts.google.com."
  type        = string
}

variable "oidc_client_id" {
  type = string
}

variable "public_url" {
  description = "External https URL. Leave null to use the Cloud Run URL (or https://<domain> when the load balancer is enabled)."
  type        = string
  default     = null
}

variable "embedding_provider" {
  description = "SATCHEL_EMBEDDING_PROVIDER. Null keeps satchel's default (fastembed, baked into the image)."
  type        = string
  default     = null
}

variable "chat_provider" {
  description = "SATCHEL_CHAT_PROVIDER (curation LLM). Null disables."
  type        = string
  default     = null
}

variable "chat_model" {
  type    = string
  default = null
}

variable "enable_chat_api_key" {
  description = "Mount the chat_api_key secret as SATCHEL_CHAT_API_KEY. Must match the foundation module's enable_chat_api_key."
  type        = bool
  default     = false
}

variable "extra_env" {
  description = "Additional plain environment variables for the service and jobs."
  type        = map(string)
  default     = {}
}

# --- Service sizing --------------------------------------------------------

variable "deploy_service" {
  description = "Create the Cloud Run service. Set false on the very first apply when the service's /readyz startup probe cannot pass yet because migrations have not run."
  type        = bool
  default     = true
}

variable "min_instances" {
  description = "Default 1: MCP clients time out on cold starts."
  type        = number
  default     = 1
}

variable "max_instances" {
  type    = number
  default = 5
}

variable "cpu" {
  type    = string
  default = "1"
}

variable "memory" {
  type    = string
  default = "2Gi"
}

variable "request_timeout_seconds" {
  description = "3600 so long-lived MCP SSE streams are not cut."
  type        = number
  default     = 3600
}

variable "concurrency" {
  type    = number
  default = 80
}

variable "startup_probe_path" {
  description = "Startup probe path. /readyz also checks DB reachability and schema version, so a revision whose migration has not run never takes traffic."
  type        = string
  default     = "/readyz"
}

variable "ingress" {
  description = "INGRESS_TRAFFIC_ALL, INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER or INGRESS_TRAFFIC_INTERNAL_ONLY. Null picks ALL, or INTERNAL_LOAD_BALANCER when the load balancer is enabled."
  type        = string
  default     = null
}

variable "allow_unauthenticated" {
  description = "Grant roles/run.invoker to allUsers. Default true because satchel authenticates every request itself (OIDC session, PAT or OAuth bearer); Cloud Run IAM in front would block MCP and OAuth clients. Organization policies that forbid allUsers need this false plus an authenticating proxy."
  type        = bool
  default     = true
}

variable "deletion_protection" {
  description = "Cloud Run deletion protection for the service."
  type        = bool
  default     = false
}

# --- Jobs and scheduling ---------------------------------------------------

variable "worker_schedule" {
  description = "Cron for `satchel worker --once`. Null disables the scheduler job."
  type        = string
  default     = "*/15 * * * *"
}

variable "audit_export_schedule" {
  description = "Cron for the daily audit export (exports the previous 24 hours). Null disables."
  type        = string
  default     = "0 3 * * *"
}

variable "audit_export_window_hours" {
  type    = number
  default = 24
}

variable "scheduler_time_zone" {
  type    = string
  default = "Etc/UTC"
}

variable "scheduler_http_headers" {
  description = "Extra HTTP headers on the scheduler requests. Empty on GCP; the floci example sets one because the emulator omits Cloud Scheduler's default headers, which hashicorp/google 6.x cannot read back."
  type        = map(string)
  default     = {}
}

variable "run_migrations_on_apply" {
  description = "Run `gcloud run jobs execute <name>-migrate --wait` from the machine running terraform whenever the image changes. Needs gcloud installed and authenticated. Default false: run the command yourself (see docs/deploy/cloud-run.md)."
  type        = bool
  default     = false
}

# --- Optional load balancer ------------------------------------------------

variable "enable_load_balancer" {
  description = "Front the service with an external HTTPS load balancer (serverless NEG, managed certificate)."
  type        = bool
  default     = false
}

variable "domain" {
  description = "DNS name for the load balancer certificate. Point an A record at the lb_ip_address output."
  type        = string
  default     = null
}

variable "enable_cloud_armor" {
  type    = bool
  default = false
}

variable "cloud_armor_allowed_ip_ranges" {
  description = "When non-empty, Cloud Armor denies everything except these CIDRs. Leave empty to allow all and only apply the rate limit."
  type        = list(string)
  default     = []
}

variable "cloud_armor_rate_limit_per_minute" {
  description = "Per-IP request rate limit; excess is answered with 429."
  type        = number
  default     = 600
}

variable "enable_iap" {
  description = "Enable Identity-Aware Proxy on the UI paths only. MCP, OAuth, auth and REST paths stay un-IAP'd so clients keep working."
  type        = bool
  default     = false
}

variable "iap_oauth_client_id" {
  type    = string
  default = null
}

variable "iap_oauth_client_secret" {
  type      = string
  default   = null
  sensitive = true
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
