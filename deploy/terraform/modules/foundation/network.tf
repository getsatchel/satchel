resource "google_compute_network" "this" {
  count = var.create_network ? 1 : 0

  project                 = var.project_id
  name                    = var.name
  auto_create_subnetworks = false

  depends_on = [google_project_service.apis]
}

resource "google_compute_subnetwork" "this" {
  count = var.create_network ? 1 : 0

  project                  = var.project_id
  name                     = var.name
  region                   = var.region
  network                  = google_compute_network.this[0].id
  ip_cidr_range            = var.subnet_cidr
  private_ip_google_access = true
}

data "google_compute_network" "existing" {
  count = var.create_network ? 0 : 1

  project = var.project_id
  name    = var.network_name
}

locals {
  network_id         = var.create_network ? google_compute_network.this[0].id : data.google_compute_network.existing[0].id
  network_name       = var.create_network ? google_compute_network.this[0].name : var.network_name
  subnetwork_name    = var.create_network ? google_compute_subnetwork.this[0].name : var.subnetwork_name
  subnetwork_id      = var.create_network ? google_compute_subnetwork.this[0].id : null
  private_service_ok = var.database_private_ip && var.create_private_service_access
}

# Private Service Access: the peering that gives Cloud SQL a private IP in the VPC.
resource "google_compute_global_address" "private_service_range" {
  count = local.private_service_ok ? 1 : 0

  project       = var.project_id
  name          = "${var.name}-private-service-range"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 20
  network       = local.network_id
}

resource "google_service_networking_connection" "private_service_access" {
  count = local.private_service_ok ? 1 : 0

  network                 = local.network_id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.private_service_range[0].name]

  depends_on = [google_project_service.apis]
}
