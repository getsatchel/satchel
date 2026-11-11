# Optional external HTTPS load balancer.
#
# Path split (one host, one Cloud Run service behind two backend services):
#   public backend (never IAP): /mcp, /mcp/*, /oauth/*, /auth/*, /.well-known/*, /v1/*, /healthz, /readyz
#   UI backend (IAP when enabled): everything else, i.e. the single-page UI at / and its assets
# MCP and OAuth clients (Claude Desktop, Claude Code, Cursor) cannot complete an IAP login, and REST
# clients send bearer tokens that IAP would reject, so only the UI shell is gated.

locals {
  lb_count      = var.enable_load_balancer ? 1 : 0
  public_paths  = ["/mcp", "/mcp/*", "/oauth/*", "/auth/*", "/.well-known/*", "/v1/*", "/healthz", "/readyz"]
  armor_enabled = var.enable_load_balancer && var.enable_cloud_armor
}

resource "google_compute_global_address" "lb" {
  count = local.lb_count

  project = var.project_id
  name    = "${var.name}-lb"

  lifecycle {
    precondition {
      condition     = var.domain != null && var.deploy_service
      error_message = "enable_load_balancer needs var.domain and deploy_service = true."
    }
  }
}

resource "google_compute_region_network_endpoint_group" "run" {
  count = local.lb_count

  project               = var.project_id
  name                  = "${var.name}-neg"
  region                = var.region
  network_endpoint_type = "SERVERLESS"

  cloud_run {
    service = google_cloud_run_v2_service.this[0].name
  }
}

resource "google_compute_security_policy" "this" {
  count = local.armor_enabled ? 1 : 0

  project = var.project_id
  name    = "${var.name}-armor"

  dynamic "rule" {
    for_each = length(var.cloud_armor_allowed_ip_ranges) > 0 ? [1] : []
    content {
      action   = "allow"
      priority = 1000
      match {
        versioned_expr = "SRC_IPS_V1"
        config {
          src_ip_ranges = var.cloud_armor_allowed_ip_ranges
        }
      }
      description = "Allowed ranges"
    }
  }

  rule {
    action   = "throttle"
    priority = 2000
    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
    rate_limit_options {
      conform_action = "allow"
      exceed_action  = "deny(429)"
      enforce_on_key = "IP"
      rate_limit_threshold {
        count        = var.cloud_armor_rate_limit_per_minute
        interval_sec = 60
      }
    }
    description = "Per-IP rate limit"
  }

  rule {
    action   = length(var.cloud_armor_allowed_ip_ranges) > 0 ? "deny(403)" : "allow"
    priority = 2147483647
    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
    description = "Default rule"
  }
}

resource "google_compute_backend_service" "public" {
  count = local.lb_count

  project               = var.project_id
  name                  = "${var.name}-public"
  protocol              = "HTTPS"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  timeout_sec           = 3600
  security_policy       = local.armor_enabled ? google_compute_security_policy.this[0].id : null

  backend {
    group = google_compute_region_network_endpoint_group.run[0].id
  }

  log_config {
    enable      = true
    sample_rate = 1
  }
}

resource "google_compute_backend_service" "ui" {
  count = local.lb_count

  project               = var.project_id
  name                  = "${var.name}-ui"
  protocol              = "HTTPS"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  timeout_sec           = 3600
  security_policy       = local.armor_enabled ? google_compute_security_policy.this[0].id : null

  backend {
    group = google_compute_region_network_endpoint_group.run[0].id
  }

  dynamic "iap" {
    for_each = var.enable_iap ? [1] : []
    content {
      enabled              = true
      oauth2_client_id     = var.iap_oauth_client_id
      oauth2_client_secret = var.iap_oauth_client_secret
    }
  }

  log_config {
    enable      = true
    sample_rate = 1
  }
}

resource "google_compute_url_map" "https" {
  count = local.lb_count

  project         = var.project_id
  name            = "${var.name}-https"
  default_service = google_compute_backend_service.ui[0].id

  host_rule {
    hosts        = [var.domain]
    path_matcher = "satchel"
  }

  path_matcher {
    name            = "satchel"
    default_service = google_compute_backend_service.ui[0].id

    path_rule {
      paths   = local.public_paths
      service = google_compute_backend_service.public[0].id
    }
  }
}

resource "google_compute_managed_ssl_certificate" "this" {
  count = local.lb_count

  project = var.project_id
  name    = "${var.name}-cert"

  managed {
    domains = [var.domain]
  }
}

resource "google_compute_target_https_proxy" "this" {
  count = local.lb_count

  project          = var.project_id
  name             = "${var.name}-https"
  url_map          = google_compute_url_map.https[0].id
  ssl_certificates = [google_compute_managed_ssl_certificate.this[0].id]
}

resource "google_compute_global_forwarding_rule" "https" {
  count = local.lb_count

  project               = var.project_id
  name                  = "${var.name}-https"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  ip_address            = google_compute_global_address.lb[0].id
  port_range            = "443"
  target                = google_compute_target_https_proxy.this[0].id
}

resource "google_compute_url_map" "redirect" {
  count = local.lb_count

  project = var.project_id
  name    = "${var.name}-redirect"

  default_url_redirect {
    https_redirect         = true
    redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
    strip_query            = false
  }
}

resource "google_compute_target_http_proxy" "redirect" {
  count = local.lb_count

  project = var.project_id
  name    = "${var.name}-redirect"
  url_map = google_compute_url_map.redirect[0].id
}

resource "google_compute_global_forwarding_rule" "http" {
  count = local.lb_count

  project               = var.project_id
  name                  = "${var.name}-http"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  ip_address            = google_compute_global_address.lb[0].id
  port_range            = "80"
  target                = google_compute_target_http_proxy.redirect[0].id
}
