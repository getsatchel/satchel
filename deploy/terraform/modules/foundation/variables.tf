variable "project_id" {
  description = "Google Cloud project that hosts satchel."
  type        = string
}

variable "region" {
  description = "Region for Cloud SQL, buckets, Artifact Registry and (via the other modules) Cloud Run or GKE."
  type        = string
}

variable "name" {
  description = "Name prefix for every resource (secrets, service accounts, buckets, instance)."
  type        = string
  default     = "satchel"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,20}$", var.name))
    error_message = "name must be 2-21 lowercase letters, digits or dashes and start with a letter."
  }
}

variable "labels" {
  description = "Labels applied to resources that support them."
  type        = map(string)
  default     = {}
}

# --- Network ---------------------------------------------------------------

variable "create_network" {
  description = "Create a dedicated VPC and subnet. Set false to use an existing network (then network_name and subnetwork_name are required)."
  type        = bool
  default     = true
}

variable "network_name" {
  description = "Existing VPC name, used only when create_network is false."
  type        = string
  default     = null
}

variable "subnetwork_name" {
  description = "Existing subnet name in var.region, used only when create_network is false."
  type        = string
  default     = null
}

variable "subnet_cidr" {
  description = "Primary range of the subnet created when create_network is true. Cloud Run Direct VPC egress needs at least a /26."
  type        = string
  default     = "10.10.0.0/24"
}

variable "create_private_service_access" {
  description = "Create the Private Service Access range and peering that private-IP Cloud SQL needs. Set false if the VPC already has one."
  type        = bool
  default     = true
}

# --- Cloud SQL -------------------------------------------------------------

variable "database_private_ip" {
  description = "Private IP only (default, via Private Service Access). Set false for a public IP with NO authorized networks: it is reachable only through the Cloud SQL connector / Auth Proxy, which authenticates with IAM."
  type        = bool
  default     = true
}

variable "database_tier" {
  description = "Cloud SQL machine tier. db-custom-1-3840 is the smallest sensible size for pgvector search."
  type        = string
  default     = "db-custom-1-3840"
}

variable "database_edition" {
  description = "ENTERPRISE or ENTERPRISE_PLUS. Custom tiers require ENTERPRISE."
  type        = string
  default     = "ENTERPRISE"
}

variable "database_availability_type" {
  description = "ZONAL (cheaper) or REGIONAL (high availability, roughly double the price)."
  type        = string
  default     = "ZONAL"

  validation {
    condition     = contains(["ZONAL", "REGIONAL"], var.database_availability_type)
    error_message = "database_availability_type must be ZONAL or REGIONAL."
  }
}

variable "database_disk_size_gb" {
  description = "Initial SSD size in GB. Storage auto-grows."
  type        = number
  default     = 10
}

variable "database_deletion_protection" {
  description = "Applies both the Terraform lifecycle guard and Cloud SQL's own deletion protection. See docs/deploy/cloud-run.md for the teardown steps."
  type        = bool
  default     = true
}

variable "database_backup_retention_count" {
  description = "Number of automated backups to keep."
  type        = number
  default     = 14
}

variable "database_pitr_log_retention_days" {
  description = "Days of write-ahead logs kept for point-in-time recovery (1-35)."
  type        = number
  default     = 7
}

variable "database_backup_start_time" {
  description = "UTC HH:MM start of the daily backup window."
  type        = string
  default     = "17:00"
}

variable "database_maintenance_day" {
  description = "Maintenance window day (1 = Monday ... 7 = Sunday)."
  type        = number
  default     = 7
}

variable "database_maintenance_hour" {
  description = "Maintenance window hour (UTC, 0-23)."
  type        = number
  default     = 18
}

variable "database_url_style" {
  description = "How the generated DB URLs reach Postgres. unix_socket: Cloud Run built-in connector (postgresql+psycopg://u:p@/satchel?host=/cloudsql/CONN). tcp: Cloud SQL Auth Proxy sidecar on 127.0.0.1:5432 (GKE)."
  type        = string
  default     = "unix_socket"

  validation {
    condition     = contains(["unix_socket", "tcp"], var.database_url_style)
    error_message = "database_url_style must be unix_socket or tcp."
  }
}

variable "database_tcp_host" {
  description = "Host in tcp-style DB URLs. Null means 127.0.0.1 (the Auth Proxy sidecar); \"instance\" uses the instance's first IP address (direct connections, for example the floci emulator)."
  type        = string
  default     = null
}

variable "database_tcp_port" {
  description = "Port in tcp-style DB URLs."
  type        = number
  default     = 5432
}

variable "create_database" {
  description = "Create the satchel database with google_sql_database. Leave true; false is only for an emulator where the database is created another way (examples/floci-*: hashicorp/google 6.x cannot address that API together with the instance on a plain-http endpoint)."
  type        = bool
  default     = true
}

# --- Secrets ---------------------------------------------------------------

variable "oidc_client_secret" {
  description = "OIDC client secret. When null the Secret Manager secret is created empty and you add a version yourself (see the deploy docs)."
  type        = string
  default     = null
  sensitive   = true
}

variable "enable_chat_api_key" {
  description = "Create a Secret Manager secret for SATCHEL_CHAT_API_KEY (curation LLM)."
  type        = bool
  default     = false
}

variable "chat_api_key" {
  description = "Chat API key value. Only used when enable_chat_api_key is true; null creates an empty secret shell."
  type        = string
  default     = null
  sensitive   = true
}

# --- Storage ---------------------------------------------------------------

variable "result_ttl_days" {
  description = "satchel's result TTL. The results bucket lifecycle deletes objects after result_ttl_days + 1."
  type        = number
  default     = 7
}

variable "bucket_location" {
  description = "Location of the buckets. Defaults to var.region."
  type        = string
  default     = null
}

variable "results_bucket_force_destroy" {
  description = "Allow terraform destroy to delete a non-empty results bucket (results are disposable)."
  type        = bool
  default     = true
}

variable "audit_bucket_force_destroy" {
  description = "Allow terraform destroy to delete a non-empty audit bucket. Leave false: audit exports are evidence."
  type        = bool
  default     = false
}

variable "audit_retention_days" {
  description = "Bucket retention policy for audit exports (objects cannot be deleted or overwritten for this many days). null disables."
  type        = number
  default     = null
}

variable "audit_retention_locked" {
  description = "Lock the retention policy. IRREVERSIBLE: the policy can never be shortened or removed."
  type        = bool
  default     = false
}

# --- Image registry --------------------------------------------------------

variable "create_ghcr_remote_repository" {
  description = "Create an Artifact Registry remote repository that proxies ghcr.io. Cloud Run cannot pull from ghcr.io directly, so keep this true unless you mirror the image yourself."
  type        = bool
  default     = true
}

variable "upstream_image_repository" {
  description = "Image repository on the upstream registry, without tag."
  type        = string
  default     = "ghcr.io/getsatchel/satchel"
}

variable "image_repository_override" {
  description = "Full image repository (no tag) to use instead of the remote repository, for example your own Artifact Registry repo. Only used when create_ghcr_remote_repository is false."
  type        = string
  default     = null
}

variable "bigquery_projects" {
  description = "Projects whose BigQuery data Satchel's warehouse connections query with Application Default Credentials. The runtime service account gets roles/bigquery.jobUser (run query jobs) and roles/bigquery.dataViewer (read tables) on each, nothing more. For tighter access leave this empty and grant dataViewer per dataset (docs/integrations/warehouses.md)."
  type        = list(string)
  default     = []
}

variable "source_artifact_buckets" {
  description = "Buckets (names) holding source artifacts that sources read with gs:// locations (dbt target/, Cube models). The runtime service account gets read-only access (roles/storage.objectViewer)."
  type        = list(string)
  default     = []
}
