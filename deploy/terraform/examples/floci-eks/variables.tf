variable "floci_endpoint" {
  description = "floci's AWS endpoint as seen from the machine running Terraform."
  type        = string
  default     = "http://localhost:4566"
}

variable "floci_host" {
  description = "floci's IP on the compose network, as seen from the k3s pods (deploy/floci-aws/floci-ip.sh)."
  type        = string
}

variable "database_proxy_port" {
  description = "Port of floci's RDS proxy for the first (only) instance."
  type        = number
  default     = 7001
}

variable "kubeconfig" {
  type    = string
  default = "../../../floci-aws/.kubeconfig"
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "name" {
  type    = string
  default = "satchel"
}

variable "image_repository" {
  type    = string
  default = "satchel"
}

variable "image_tag" {
  type    = string
  default = "floci"
}

variable "domain" {
  type    = string
  default = "satchel.floci.test"
}

variable "oidc_issuer" {
  type    = string
  default = "https://idp.floci.test"
}

variable "oidc_client_id" {
  type    = string
  default = "satchel"
}

variable "oidc_client_secret" {
  type      = string
  default   = "floci-oidc-secret"
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
