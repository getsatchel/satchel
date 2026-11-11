variable "name" {
  description = "Name prefix for every resource (secrets, roles, buckets, the database)."
  type        = string
  default     = "satchel"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,20}$", var.name))
    error_message = "name must be 2-21 lowercase letters, digits or dashes and start with a letter."
  }
}

variable "tags" {
  description = "Tags applied to every resource that supports them (on top of the provider's default_tags)."
  type        = map(string)
  default     = {}
}

# --- Network ---------------------------------------------------------------

variable "create_vpc" {
  description = "Create a dedicated VPC (public subnets for the ALB, private subnets for EKS and RDS, NAT). Set false to use an existing VPC: then vpc_id and private_subnet_ids are required, and public_subnet_ids for an internet-facing ALB."
  type        = bool
  default     = true
}

variable "vpc_id" {
  description = "Existing VPC, used only when create_vpc is false."
  type        = string
  default     = null
}

variable "private_subnet_ids" {
  description = "Existing private subnets (at least two AZs), used only when create_vpc is false. Tag them kubernetes.io/role/internal-elb = 1 for internal ALBs."
  type        = list(string)
  default     = []
}

variable "public_subnet_ids" {
  description = "Existing public subnets, used only when create_vpc is false. Tag them kubernetes.io/role/elb = 1 so the AWS Load Balancer Controller can place an internet-facing ALB."
  type        = list(string)
  default     = []
}

variable "vpc_cidr" {
  description = "CIDR of the VPC created when create_vpc is true."
  type        = string
  default     = "10.40.0.0/16"
}

variable "az_count" {
  description = "Availability zones to spread subnets over (2 or 3)."
  type        = number
  default     = 3

  validation {
    condition     = var.az_count >= 2 && var.az_count <= 3
    error_message = "az_count must be 2 or 3."
  }
}

variable "availability_zones" {
  description = "Explicit AZ names. Null picks the first az_count available zones in the region."
  type        = list(string)
  default     = null
}

variable "single_nat_gateway" {
  description = "One NAT gateway for all private subnets (cheaper). false creates one per AZ (survives an AZ outage)."
  type        = bool
  default     = true
}

variable "cluster_name" {
  description = "EKS cluster name the subnets are tagged for (kubernetes.io/cluster/<name> = shared). Defaults to var.name."
  type        = string
  default     = null
}

# --- Encryption ------------------------------------------------------------

variable "kms_key_arn" {
  description = "Existing customer managed KMS key for RDS, S3, Secrets Manager and EKS secrets. Null creates one."
  type        = string
  default     = null
}

# --- RDS -------------------------------------------------------------------

variable "database_engine_version" {
  description = "RDS for PostgreSQL major or major.minor version. pgvector is available on every supported 17.x."
  type        = string
  default     = "17"
}

variable "database_instance_class" {
  description = "RDS instance class. db.t4g.medium is the smallest sensible size for pgvector search; use db.m7g/r7g for production load."
  type        = string
  default     = "db.t4g.medium"
}

variable "database_multi_az" {
  description = "Multi-AZ standby (high availability, roughly double the price)."
  type        = bool
  default     = false
}

variable "database_allocated_storage_gb" {
  description = "Initial gp3 storage in GB."
  type        = number
  default     = 20
}

variable "database_max_allocated_storage_gb" {
  description = "Storage autoscaling ceiling in GB (0 disables autoscaling)."
  type        = number
  default     = 200
}

variable "database_deletion_protection" {
  description = "RDS deletion protection, and a final snapshot on destroy. See docs/deploy/eks.md for teardown."
  type        = bool
  default     = true
}

variable "database_backup_retention_days" {
  description = "Automated backup (and point-in-time recovery) retention, 1-35 days."
  type        = number
  default     = 14
}

variable "database_backup_window" {
  description = "UTC hh24:mi-hh24:mi daily backup window."
  type        = string
  default     = "17:00-17:30"
}

variable "database_maintenance_window" {
  description = "Weekly maintenance window (ddd:hh24:mi-ddd:hh24:mi, UTC). Must not overlap the backup window."
  type        = string
  default     = "sun:18:00-sun:19:00"
}

variable "database_performance_insights" {
  description = "Enable Performance Insights (7 days free retention)."
  type        = bool
  default     = true
}

variable "database_allowed_security_group_ids" {
  description = "Security groups allowed to reach Postgres on 5432 (the EKS module adds the cluster's own rule; use this for bastions or other workloads)."
  type        = list(string)
  default     = []
}

variable "database_allowed_cidrs" {
  description = "CIDRs allowed to reach Postgres on 5432. Leave empty in production; the emulator example uses it."
  type        = list(string)
  default     = []
}

variable "database_host_override" {
  description = "Host:port written into the generated DB URLs instead of the RDS endpoint (emulators publish the database elsewhere). Null uses the RDS endpoint."
  type        = string
  default     = null
}

variable "database_sslmode" {
  description = "libpq sslmode in the generated DB URLs. require (default) encrypts; verify-full also needs the RDS CA bundle in the image. disable only for an emulator."
  type        = string
  default     = "require"

  validation {
    condition     = contains(["disable", "require", "verify-ca", "verify-full"], var.database_sslmode)
    error_message = "database_sslmode must be disable, require, verify-ca or verify-full."
  }
}

# --- Secrets ---------------------------------------------------------------

variable "oidc_client_secret" {
  description = "OIDC client secret. When null the Secrets Manager secret is created empty; put a value with `aws secretsmanager put-secret-value` (see the deploy docs)."
  type        = string
  default     = null
  sensitive   = true
}

variable "enable_chat_api_key" {
  description = "Create a Secrets Manager secret for SATCHEL_CHAT_API_KEY (curation LLM)."
  type        = bool
  default     = false
}

variable "chat_api_key" {
  description = "Chat API key value. Only used when enable_chat_api_key is true; null creates an empty secret."
  type        = string
  default     = null
  sensitive   = true
}

variable "secret_recovery_window_days" {
  description = "Secrets Manager recovery window on delete (0 deletes immediately; 7-30 otherwise)."
  type        = number
  default     = 7
}

# --- Storage ---------------------------------------------------------------

variable "result_ttl_days" {
  description = "satchel's result TTL. The results bucket lifecycle expires objects after result_ttl_days + 1."
  type        = number
  default     = 7
}

variable "bucket_name_prefix" {
  description = "Prefix of the bucket names (globally unique). Null uses <account id>-<name>."
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
  description = "S3 Object Lock default retention for audit exports, in days. null disables Object Lock (versioning stays on). Object Lock can only be enabled when the bucket is created."
  type        = number
  default     = null
}

variable "audit_retention_mode" {
  description = "GOVERNANCE (privileged users can shorten it) or COMPLIANCE (IRREVERSIBLE: nobody, including root, can delete objects before retention ends)."
  type        = string
  default     = "GOVERNANCE"

  validation {
    condition     = contains(["GOVERNANCE", "COMPLIANCE"], var.audit_retention_mode)
    error_message = "audit_retention_mode must be GOVERNANCE or COMPLIANCE."
  }
}

variable "s3_kms_encryption" {
  description = "Encrypt the buckets with the KMS key (SSE-KMS with S3 Bucket Keys). false uses SSE-S3."
  type        = bool
  default     = true
}

variable "source_artifact_buckets" {
  description = "S3 buckets (names) holding source artifacts that sources read with s3:// locations (dbt target/, Cube models). The runtime policy gets s3:GetObject and s3:ListBucket on them (add kms:Decrypt on their key yourself if they use SSE-KMS with another key)."
  type        = list(string)
  default     = []
}
