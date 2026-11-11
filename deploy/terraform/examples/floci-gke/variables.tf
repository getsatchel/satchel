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
  type    = number
  default = 4588
}

variable "storage_emulator_host" {
  description = "STORAGE_EMULATOR_HOST for the pods. k3s pods cannot resolve Docker names, so use floci's IP on the floci-satchel network: http://$(docker inspect -f '{{(index .NetworkSettings.Networks \"floci-satchel\").IPAddress}}' floci-satchel-gcp):4588. Empty leaves it unset (result reads/writes then fail; the web pod still becomes Ready)."
  type        = string
  default     = ""
}

variable "image_repository" {
  description = "Local image (no tag), imported into k3s with deploy/floci/k3s-load-image.sh."
  type        = string
  default     = "satchel"
}

variable "image_tag" {
  type    = string
  default = "floci"
}

variable "domain" {
  description = "Only used for SATCHEL_PUBLIC_URL (https://<domain>); nothing is exposed."
  type        = string
  default     = "satchel.floci.test"
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

variable "first_workspace" {
  type = object({
    slug        = string
    name        = string
    admin_email = string
  })
  default = {
    slug        = "demo"
    name        = "Demo"
    admin_email = "admin@example.com"
  }
}
