resource "google_storage_bucket" "results" {
  project                     = var.project_id
  name                        = "${var.project_id}-${var.name}-results"
  location                    = local.bucket_location
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = var.results_bucket_force_destroy
  labels                      = local.labels

  lifecycle_rule {
    condition {
      age = var.result_ttl_days + 1
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_storage_bucket" "audit" {
  project                     = var.project_id
  name                        = "${var.project_id}-${var.name}-audit"
  location                    = local.bucket_location
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = var.audit_bucket_force_destroy
  labels                      = local.labels

  versioning {
    enabled = true
  }

  dynamic "retention_policy" {
    for_each = var.audit_retention_days == null ? [] : [var.audit_retention_days]
    content {
      retention_period = retention_policy.value * 86400
      is_locked        = var.audit_retention_locked
    }
  }

  depends_on = [google_project_service.apis]
}
