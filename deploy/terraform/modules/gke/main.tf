locals {
  labels     = merge({ app = "satchel", managed-by = "terraform" }, var.labels)
  chart_path = coalesce(var.chart_path, "${path.module}/../../../helm/satchel")
  ksa_name   = "satchel"

  public_url = "https://${var.domain}"
}

resource "google_project_service" "apis" {
  for_each = toset([
    "container.googleapis.com",
    "certificatemanager.googleapis.com",
    "secretmanager.googleapis.com",
  ])

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# --- Cluster ---------------------------------------------------------------

resource "google_container_cluster" "this" {
  project  = var.project_id
  name     = var.name
  location = var.region

  enable_autopilot    = true
  deletion_protection = var.cluster_deletion_protection
  network             = var.network_id
  subnetwork          = var.subnetwork_id
  resource_labels     = local.labels

  release_channel {
    channel = var.release_channel
  }

  # Autopilot manages secondary ranges for the subnet when this block is empty.
  ip_allocation_policy {}

  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = false
  }

  dynamic "master_authorized_networks_config" {
    for_each = length(var.master_authorized_networks) > 0 ? [1] : []
    content {
      dynamic "cidr_blocks" {
        for_each = var.master_authorized_networks
        content {
          display_name = cidr_blocks.key
          cidr_block   = cidr_blocks.value
        }
      }
    }
  }

  # Workload Identity is always on in Autopilot; declared so plans stay stable.
  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  # Gateway API for the external HTTPS load balancer.
  gateway_api_config {
    channel = "CHANNEL_STANDARD"
  }

  # Secret Manager CSI add-on: lets pods mount Secret Manager secrets with a SecretProviderClass.
  secret_manager_config {
    enabled = true
  }

  # Autopilot has no cluster-level initial_node_count and this module never sets it, so this is a
  # no-op on GCP. It keeps a local emulator that reports a default count (floci-gcp reports 3)
  # from forcing a cluster replacement on every plan (docs/deploy/floci.md).
  lifecycle {
    ignore_changes = [initial_node_count]
  }

  depends_on = [google_project_service.apis]
}

resource "google_compute_router" "this" {
  count = var.create_nat ? 1 : 0

  project = var.project_id
  name    = "${var.name}-nat"
  region  = var.region
  network = var.router_network_name
}

resource "google_compute_router_nat" "this" {
  count = var.create_nat ? 1 : 0

  project                            = var.project_id
  name                               = "${var.name}-nat"
  region                             = var.region
  router                             = google_compute_router.this[0].name
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"
}

# --- Workload Identity: KSA <-> runtime GSA -------------------------------

resource "google_service_account_iam_member" "workload_identity" {
  service_account_id = var.runtime_service_account_name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.namespace}/${local.ksa_name}]"

  depends_on = [google_container_cluster.this]
}

# --- Ingress: managed certificate (the chart creates the Gateway) ---------------------------

resource "google_certificate_manager_certificate" "this" {
  count = var.enable_certificate_manager ? 1 : 0

  project = var.project_id
  name    = "${var.name}-cert"
  labels  = local.labels

  managed {
    domains = [var.domain]
  }

  depends_on = [google_project_service.apis]
}

resource "google_certificate_manager_certificate_map" "this" {
  count = var.enable_certificate_manager ? 1 : 0

  project = var.project_id
  name    = "${var.name}-certmap"
  labels  = local.labels
}

resource "google_certificate_manager_certificate_map_entry" "this" {
  count = var.enable_certificate_manager ? 1 : 0

  project      = var.project_id
  name         = "${var.name}-certmap-entry"
  map          = google_certificate_manager_certificate_map.this[0].name
  certificates = [google_certificate_manager_certificate.this[0].id]
  hostname     = var.domain
}

# --- Kubernetes Secret consumed by the chart ------------------------------

# A stable address so the domain's A record can be set before (and survive) Gateway changes.
resource "google_compute_global_address" "gateway" {
  project = var.project_id
  name    = "${var.name}-gateway"

  depends_on = [google_project_service.apis]
}

resource "kubernetes_namespace_v1" "this" {
  metadata {
    name = var.namespace
  }

  depends_on = [google_container_cluster.this]
}

resource "kubernetes_secret_v1" "satchel" {
  metadata {
    name      = "${var.name}-secrets"
    namespace = kubernetes_namespace_v1.this.metadata[0].name
  }

  # Keys are the SATCHEL_* environment variable names (the chart's existingSecret contract).
  data = merge(
    {
      SATCHEL_DATABASE_URL           = var.database_urls["database_url"]
      SATCHEL_ADMIN_DATABASE_URL     = var.database_urls["admin_database_url"]
      SATCHEL_MIGRATION_DATABASE_URL = var.database_urls["migration_database_url"]
      SATCHEL_OIDC_CLIENT_SECRET     = var.oidc_client_secret
    },
    var.chat_api_key == null ? {} : { SATCHEL_CHAT_API_KEY = var.chat_api_key },
    var.encryption_key == null ? {} : { SATCHEL_ENCRYPTION_KEY = var.encryption_key },
  )
}

# Kept apart from the runtime Secret so the owner credential never reaches web or worker pods
# (the chart's bootstrap.existingSecret).
resource "kubernetes_secret_v1" "bootstrap" {
  metadata {
    name      = "${var.name}-bootstrap"
    namespace = kubernetes_namespace_v1.this.metadata[0].name
  }

  data = {
    SATCHEL_BOOTSTRAP_DATABASE_URL = var.database_urls["migration_database_url"]
    SATCHEL_APP_DB_PASSWORD        = var.database_passwords["app"]
    SATCHEL_WORKER_DB_PASSWORD     = var.database_passwords["worker"]
  }
}

module "values" {
  source = "../helm-values"

  image_repository          = var.image_repository
  image_tag                 = var.image_tag
  public_url                = local.public_url
  domain                    = var.domain
  existing_secret_name      = kubernetes_secret_v1.satchel.metadata[0].name
  bootstrap_enabled         = var.bootstrap_enabled
  bootstrap_secret_name     = kubernetes_secret_v1.bootstrap.metadata[0].name
  service_account_name      = local.ksa_name
  gcp_service_account_email = var.runtime_service_account_email
  instance_connection_name  = var.instance_connection_name
  results_bucket            = var.results_bucket
  oidc_issuer               = var.oidc_issuer
  oidc_client_id            = var.oidc_client_id
  chat_provider             = var.chat_provider
  chat_model                = var.chat_model
  session_retention_days    = var.session_retention_days
  replicas                  = var.replicas
  certificate_map           = var.enable_certificate_manager ? google_certificate_manager_certificate_map.this[0].name : null
  static_ip_name            = google_compute_global_address.gateway.name
  first_workspace           = var.first_workspace
  extra_values              = var.extra_values
}

resource "helm_release" "satchel" {
  name             = var.name
  namespace        = kubernetes_namespace_v1.this.metadata[0].name
  create_namespace = false
  chart            = local.chart_path
  atomic           = true
  timeout          = 900

  values = [module.values.values_yaml]

  depends_on = [
    google_service_account_iam_member.workload_identity,
    google_certificate_manager_certificate_map_entry.this,
    google_compute_router_nat.this,
  ]
}
