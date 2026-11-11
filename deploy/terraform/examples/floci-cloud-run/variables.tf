variable "project_id" {
  type    = string
  default = "floci-local"
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "name" {
  type    = string
  default = "satchel"
}

variable "floci_endpoint" {
  description = "floci-gcp API endpoint as seen from the machine running terraform."
  type        = string
  default     = "http://localhost:4588"
}

variable "floci_port" {
  description = "Port of the floci front door in the generated Cloud Run URL."
  type        = number
  default     = 4588
}

variable "storage_emulator_host" {
  description = "STORAGE_EMULATOR_HOST for the workloads: floci as seen from containers on its Docker network."
  type        = string
  default     = "http://floci-gcp:4588"
}

variable "image_repository" {
  description = "Local image (no tag). floci runs it through the host Docker daemon, so a local build works."
  type        = string
  default     = "satchel"
}

variable "image_tag" {
  type    = string
  default = "floci"
}

variable "oidc_issuer" {
  description = "Never contacted by these tests; SATCHEL_ENV=production only requires OIDC to be configured."
  type        = string
  default     = "https://accounts.google.com"
}

variable "oidc_client_id" {
  type    = string
  default = "floci-test-client"
}

variable "oidc_client_secret" {
  type      = string
  default   = "floci-test-secret"
  sensitive = true
}

variable "deploy_service" {
  description = "false creates everything but the web service (run migrations first), as in production."
  type        = bool
  default     = true
}
