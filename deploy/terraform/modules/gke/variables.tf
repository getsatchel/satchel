variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

variable "name" {
  description = "Cluster and release name."
  type        = string
  default     = "satchel"
}

variable "labels" {
  type    = map(string)
  default = {}
}

# --- Foundation inputs -----------------------------------------------------

variable "network_id" {
  type = string
}

variable "subnetwork_id" {
  type = string
}

variable "router_network_name" {
  description = "VPC name for the Cloud NAT router."
  type        = string
}

variable "instance_connection_name" {
  type = string
}

variable "runtime_service_account_email" {
  description = "foundation runtime service account; the Kubernetes service account is bound to it with Workload Identity."
  type        = string
}

variable "runtime_service_account_name" {
  description = "Fully qualified name (projects/.../serviceAccounts/...) of the same account."
  type        = string
}

variable "image_repository" {
  description = "Image repository (no tag). GKE nodes can pull ghcr.io directly through Cloud NAT, so the upstream ghcr.io/getsatchel/satchel is fine here."
  type        = string
  default     = "ghcr.io/getsatchel/satchel"
}

variable "image_tag" {
  type = string
}

variable "results_bucket" {
  type = string
}

variable "database_urls" {
  description = "foundation database_url_secrets output; must be built with database_url_style = \"tcp\" for the Auth Proxy sidecar."
  type        = map(string)
  sensitive   = true
}

variable "database_passwords" {
  description = "foundation database_passwords output."
  type        = map(string)
  sensitive   = true
}

# --- Cluster ---------------------------------------------------------------

variable "cluster_deletion_protection" {
  type    = bool
  default = true
}

variable "release_channel" {
  type    = string
  default = "REGULAR"
}

variable "master_authorized_networks" {
  description = "CIDRs allowed to reach the public control-plane endpoint (kubectl, and Terraform's helm provider). Empty keeps GKE's default of allowing all."
  type        = map(string)
  default     = {}
}

variable "create_nat" {
  description = "Cloud Router and NAT so private nodes can pull ghcr.io and reach the OIDC issuer."
  type        = bool
  default     = true
}

# --- App config ------------------------------------------------------------

variable "chart_path" {
  description = "Local path of the Helm chart."
  type        = string
  default     = null
}

variable "namespace" {
  type    = string
  default = "satchel"
}

variable "domain" {
  description = "Public hostname. satchel is served at https://<domain>."
  type        = string
}

variable "oidc_issuer" {
  type = string
}

variable "oidc_client_id" {
  type = string
}

variable "oidc_client_secret" {
  type      = string
  sensitive = true
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

variable "bootstrap_enabled" {
  description = "Run `satchel db bootstrap` in the migrate hook. Needed on first install; set false afterwards to stop mounting the owner credential."
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
  description = "Create the first workspace with a post-install Job."
  type = object({
    slug        = string
    name        = string
    admin_email = string
  })
  default = null
}

variable "replicas" {
  type    = number
  default = 2
}

variable "extra_values" {
  description = "Extra Helm values (chart keys), merged over the generated ones."
  type        = any
  default     = {}
}

variable "enable_certificate_manager" {
  description = "Create a Certificate Manager managed certificate for the domain (DNS-authorization-free, load-balancer authorization) and attach it to the Gateway."
  type        = bool
  default     = true
}

variable "session_retention_days" {
  description = "SATCHEL_SESSION_RETENTION_DAYS: delete agent sessions older than this many days (7-3650; sessions with feedback are kept). Null keeps them forever. Workspace admins can override it in Settings -> Limits."
  type        = number
  default     = null
}
