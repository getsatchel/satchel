locals {
  labels = merge({ app = "satchel", managed-by = "terraform" }, var.labels)

  # Cloud Run v2 URLs are deterministic: https://<service>-<project number>.<region>.run.app
  default_url = "https://${var.name}-${var.project_number}.${var.region}.run.app"
  public_url = var.public_url != null ? var.public_url : (
    var.enable_load_balancer ? "https://${var.domain}" : local.default_url
  )

  ingress = var.ingress != null ? var.ingress : (
    var.enable_load_balancer ? "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER" : "INGRESS_TRAFFIC_ALL"
  )

  sql_volume = "cloudsql"

  plain_env = merge(
    {
      SATCHEL_ENV        = "production"
      SATCHEL_LOG_FORMAT = "json"
      SATCHEL_PUBLIC_URL = local.public_url
      SATCHEL_RESULT_DIR = "gs://${var.results_bucket}/results"

      SATCHEL_OIDC_ISSUER    = var.oidc_issuer
      SATCHEL_OIDC_CLIENT_ID = var.oidc_client_id
    },
    var.embedding_provider == null ? {} : { SATCHEL_EMBEDDING_PROVIDER = var.embedding_provider },
    var.chat_provider == null ? {} : { SATCHEL_CHAT_PROVIDER = var.chat_provider },
    var.chat_model == null ? {} : { SATCHEL_CHAT_MODEL = var.chat_model },
    var.session_retention_days == null ? {} : { SATCHEL_SESSION_RETENTION_DAYS = tostring(var.session_retention_days) },
    var.extra_env,
  )

  # env var -> secret key (a key of var.secret_ids). With runtime_profile = "gcp" each becomes a
  # Secret Manager reference (latest version); with "emulator" a plain value from
  # var.emulator_secret_values.
  common_secret_env = merge(
    {
      SATCHEL_DATABASE_URL       = "database_url"
      SATCHEL_ADMIN_DATABASE_URL = "admin_database_url"
      SATCHEL_OIDC_CLIENT_SECRET = "oidc_client_secret"
    },
    var.enable_chat_api_key ? { SATCHEL_CHAT_API_KEY = "chat_api_key" } : {},
    contains(keys(var.secret_ids), "encryption_key") ? { SATCHEL_ENCRYPTION_KEY = "encryption_key" } : {},
  )

  # Owner credentials reach the migrate job only. The bootstrap URL is the owner URL: the
  # connecting admin user becomes the schema owner.
  migrate_secret_env = merge(local.common_secret_env, {
    SATCHEL_MIGRATION_DATABASE_URL = "migration_database_url"
    SATCHEL_BOOTSTRAP_DATABASE_URL = "migration_database_url"
    SATCHEL_APP_DB_PASSWORD        = "app_password"
    SATCHEL_WORKER_DB_PASSWORD     = "worker_password"
  })

  emulator = var.runtime_profile == "emulator"
  # Cloud SQL through the built-in connector (unix socket under /cloudsql). The emulator has no
  # connector; its DB URLs are plain TCP (foundation database_url_style = "tcp").
  sql_volume_enabled = !local.emulator

  vpc_enabled = var.direct_vpc_egress && var.network_name != null
}

# --- Web service -----------------------------------------------------------

resource "google_cloud_run_v2_service" "this" {
  count = var.deploy_service ? 1 : 0

  project             = var.project_id
  name                = var.name
  location            = var.region
  ingress             = local.ingress
  deletion_protection = var.deletion_protection
  labels              = local.labels

  template {
    service_account                  = var.service_account_email
    timeout                          = "${var.request_timeout_seconds}s"
    max_instance_request_concurrency = var.concurrency
    labels                           = local.labels

    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.max_instances
    }

    dynamic "vpc_access" {
      for_each = local.vpc_enabled ? [1] : []
      content {
        egress = "PRIVATE_RANGES_ONLY"
        network_interfaces {
          network    = var.network_name
          subnetwork = var.subnetwork_name
        }
      }
    }

    dynamic "volumes" {
      for_each = local.sql_volume_enabled ? [1] : []
      content {
        name = local.sql_volume
        cloud_sql_instance {
          instances = [var.instance_connection_name]
        }
      }
    }

    containers {
      image = var.image
      args  = ["serve", "api", "--host", "0.0.0.0"]

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = var.cpu
          memory = var.memory
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }

      dynamic "volume_mounts" {
        for_each = local.sql_volume_enabled ? [1] : []
        content {
          name       = local.sql_volume
          mount_path = "/cloudsql"
        }
      }

      dynamic "env" {
        for_each = local.plain_env
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = local.emulator ? {} : local.common_secret_env
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = var.secret_ids[env.value]
              version = "latest"
            }
          }
        }
      }

      dynamic "env" {
        for_each = local.emulator ? local.common_secret_env : {}
        content {
          name  = env.key
          value = var.emulator_secret_values[env.value]
        }
      }

      dynamic "startup_probe" {
        for_each = local.emulator ? [] : [1]
        content {
          initial_delay_seconds = 5
          period_seconds        = 5
          timeout_seconds       = 3
          failure_threshold     = 36
          http_get {
            path = var.startup_probe_path
          }
        }
      }

      liveness_probe {
        period_seconds    = 30
        timeout_seconds   = 3
        failure_threshold = 3
        http_get {
          path = "/healthz"
        }
      }
    }
  }

  depends_on = [terraform_data.migrate]
}

# satchel authenticates every request itself (OIDC session cookie, PAT, OAuth access token), and MCP /
# OAuth clients cannot present Google identity tokens, so the invoker role is public.
resource "google_cloud_run_v2_service_iam_member" "public" {
  count = var.deploy_service && var.allow_unauthenticated ? 1 : 0

  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.this[0].name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
