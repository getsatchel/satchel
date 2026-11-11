# One runtime identity for the web service and the jobs. Least privilege: it can reach
# Cloud SQL, read satchel's own secrets, use the two buckets and write telemetry. Nothing else.
resource "google_service_account" "runtime" {
  project      = var.project_id
  account_id   = "${var.name}-runtime"
  display_name = "satchel runtime"

  depends_on = [google_project_service.apis]
}

locals {
  runtime_project_roles = [
    "roles/cloudsql.client",
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/cloudtrace.agent",
  ]
}

resource "google_project_iam_member" "runtime" {
  for_each = toset(local.runtime_project_roles)

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.runtime.email}"
}

# Owner-level secrets (the schema owner's credentials and the role passwords bootstrap sets) are
# readable only by the migrate identity, never by the web service or the worker.
locals {
  owner_secret_keys = ["owner_password", "migration_database_url", "app_password", "worker_password"]
}

resource "google_secret_manager_secret_iam_member" "runtime_generated" {
  for_each = { for k, v in google_secret_manager_secret.generated : k => v if !contains(local.owner_secret_keys, k) }

  project   = var.project_id
  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_secret_manager_secret_iam_member" "runtime_shell" {
  for_each = google_secret_manager_secret.shell

  project   = var.project_id
  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_storage_bucket_iam_member" "runtime_results" {
  bucket = google_storage_bucket.results.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.runtime.email}"
}

# Read-only access to buckets holding source artifacts (gs:// locations on sources).
resource "google_storage_bucket_iam_member" "runtime_source_artifacts" {
  for_each = toset(var.source_artifact_buckets)

  bucket = each.value
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.runtime.email}"
}

# Optional: BigQuery warehouse connections that use ADC (workload identity). Read-only: run
# query jobs and read tables, in the listed projects only.
locals {
  runtime_bigquery_grants = {
    for pair in setproduct(var.bigquery_projects, ["roles/bigquery.jobUser", "roles/bigquery.dataViewer"]) :
    "${pair[0]}/${pair[1]}" => { project = pair[0], role = pair[1] }
  }
}

resource "google_project_iam_member" "runtime_bigquery" {
  for_each = local.runtime_bigquery_grants

  project = each.value.project
  role    = each.value.role
  member  = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_storage_bucket_iam_member" "runtime_audit" {
  bucket = google_storage_bucket.audit.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.runtime.email}"
}

# The migrate job's identity: Cloud SQL, logs and every satchel secret, including the owner-level
# ones. Only the migrate job runs as it.
resource "google_service_account" "migrate" {
  project      = var.project_id
  account_id   = "${var.name}-migrate"
  display_name = "satchel migrate"

  depends_on = [google_project_service.apis]
}

resource "google_project_iam_member" "migrate" {
  for_each = toset(["roles/cloudsql.client", "roles/logging.logWriter"])

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.migrate.email}"
}

resource "google_secret_manager_secret_iam_member" "migrate_generated" {
  for_each = google_secret_manager_secret.generated

  project   = var.project_id
  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.migrate.email}"
}

resource "google_secret_manager_secret_iam_member" "migrate_shell" {
  for_each = google_secret_manager_secret.shell

  project   = var.project_id
  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.migrate.email}"
}
