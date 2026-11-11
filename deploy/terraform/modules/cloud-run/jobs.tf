# Jobs share one shape: same image, runtime identity, Cloud SQL socket and environment.

locals {
  # The migrate job reads owner-level secrets; it runs as its own identity when one is given.
  migrate_sa = coalesce(var.migrate_service_account_email, var.service_account_email)

  jobs = {
    migrate = {
      # bootstrap is idempotent; upgrade is a no-op at head.
      command     = ["sh"]
      args        = ["-c", "satchel db bootstrap && satchel db upgrade"]
      timeout     = "900s"
      max_retries = 0
      secret_env  = local.migrate_secret_env
      sa          = local.migrate_sa
    }
    worker = {
      command     = ["satchel"]
      args        = ["worker", "--once"]
      timeout     = "1800s"
      max_retries = 1
      secret_env  = local.common_secret_env
      sa          = var.service_account_email
    }
    audit-export = {
      command = ["sh"]
      args = [
        "-c",
        "exec satchel audit export --since \"$(date -u -d '-${var.audit_export_window_hours} hours' +%Y-%m-%dT%H:%M:%SZ)\" --to gs://${var.audit_bucket}/audit/$(date -u +%Y/%m/%d)/",
      ]
      timeout     = "1800s"
      max_retries = 1
      secret_env  = local.common_secret_env
      sa          = var.service_account_email
    }
  }
}

resource "google_cloud_run_v2_job" "this" {
  for_each = local.jobs

  project             = var.project_id
  name                = "${var.name}-${each.key}"
  location            = var.region
  deletion_protection = var.deletion_protection
  labels              = local.labels

  template {
    labels = local.labels

    template {
      service_account = each.value.sa
      timeout         = each.value.timeout
      max_retries     = each.value.max_retries

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
        image   = var.image
        command = each.value.command
        args    = each.value.args

        resources {
          limits = {
            cpu    = var.cpu
            memory = var.memory
          }
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
          for_each = local.emulator ? {} : each.value.secret_env
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
          for_each = local.emulator ? each.value.secret_env : {}
          content {
            name  = env.key
            value = var.emulator_secret_values[env.value]
          }
        }
      }
    }
  }
}

# Optional: run migrations from the machine that runs terraform, before the service rolls out.
# Off by default because it needs gcloud and credentials locally. The manual command is
#   gcloud run jobs execute <name>-migrate --region <region> --project <project> --wait
resource "terraform_data" "migrate" {
  count = var.run_migrations_on_apply ? 1 : 0

  triggers_replace = [var.image]

  provisioner "local-exec" {
    command = "gcloud run jobs execute ${google_cloud_run_v2_job.this["migrate"].name} --region ${var.region} --project ${var.project_id} --wait"
  }
}

# --- Cloud Scheduler -------------------------------------------------------

resource "google_service_account" "scheduler" {
  project      = var.project_id
  account_id   = "${var.name}-scheduler"
  display_name = "satchel Cloud Scheduler"
}

locals {
  scheduled = { for k, v in {
    worker       = var.worker_schedule
    audit-export = var.audit_export_schedule
  } : k => v if v != null }
}

resource "google_cloud_run_v2_job_iam_member" "scheduler" {
  for_each = local.scheduled

  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_job.this[each.key].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

# The Cloud Run Admin API wants an OAuth access token, not an OIDC ID token, so the scheduler
# job uses oauth_token even though it runs as a dedicated service account.
resource "google_cloud_scheduler_job" "this" {
  for_each = local.scheduled

  project          = var.project_id
  region           = var.region
  name             = "${var.name}-${each.key}"
  description      = "Runs the ${var.name}-${each.key} Cloud Run job"
  schedule         = each.value
  time_zone        = var.scheduler_time_zone
  attempt_deadline = "180s"

  retry_config {
    retry_count = 1
  }

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.this[each.key].name}:run"
    headers     = var.scheduler_http_headers

    oauth_token {
      service_account_email = google_service_account.scheduler.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.scheduler]
}
