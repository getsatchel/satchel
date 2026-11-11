# Passwords avoid special characters so they can sit in a URL without percent-encoding.
resource "random_password" "owner" {
  length  = 32
  special = false
}

resource "random_password" "app" {
  length  = 32
  special = false
}

resource "random_password" "worker" {
  length  = 32
  special = false
}

# SATCHEL_ENCRYPTION_KEY: AES-256-GCM key for secrets satchel stores in Postgres (LLM API keys
# added in Settings → Models). 256 bits as 64 hex characters. Kept stable across applies; to
# rotate, follow docs/deploy/configuration.md (taint this resource, keep the old value in
# SATCHEL_ENCRYPTION_KEY_PREVIOUS, run `satchel secrets rotate`).
resource "random_id" "encryption_key" {
  byte_length = 32
}

locals {
  db_tcp_host = var.database_tcp_host == null ? "127.0.0.1" : (
    var.database_tcp_host == "instance" ? google_sql_database_instance.this.ip_address[0].ip_address : var.database_tcp_host
  )
  db_host_part = var.database_url_style == "unix_socket" ? "@/satchel?host=/cloudsql/${google_sql_database_instance.this.connection_name}" : "@${local.db_tcp_host}:${var.database_tcp_port}/satchel"

  database_urls = {
    # satchel_app: request traffic, row-level security applies.
    database_url = "postgresql+psycopg://satchel_app:${random_password.app.result}${local.db_host_part}"
    # satchel_worker: server-internal jobs.
    admin_database_url = "postgresql+psycopg://satchel_worker:${random_password.worker.result}${local.db_host_part}"
    # owner: migrations. Also used as the bootstrap URL (the owner is the schema owner).
    migration_database_url = "postgresql+psycopg://satchel:${random_password.owner.result}${local.db_host_part}"
  }

  secret_values = {
    owner_password         = random_password.owner.result
    app_password           = random_password.app.result
    worker_password        = random_password.worker.result
    database_url           = local.database_urls.database_url
    admin_database_url     = local.database_urls.admin_database_url
    migration_database_url = local.database_urls.migration_database_url
    encryption_key         = random_id.encryption_key.hex
  }

  generated_keys = ["owner_password", "app_password", "worker_password", "database_url", "admin_database_url", "migration_database_url", "encryption_key"]

  # Secrets created as shells; a version is added only when a value was supplied.
  shell_keys = concat(["oidc_client_secret"], var.enable_chat_api_key ? ["chat_api_key"] : [])
  shell_values = {
    oidc_client_secret = var.oidc_client_secret
    chat_api_key       = var.chat_api_key
  }
  shell_has_value = {
    oidc_client_secret = nonsensitive(var.oidc_client_secret != null)
    chat_api_key       = nonsensitive(var.chat_api_key != null)
  }

  secret_ids = {
    for k in concat(local.generated_keys, local.shell_keys) :
    k => "${var.name}-${replace(k, "_", "-")}"
  }
}

resource "google_secret_manager_secret" "generated" {
  for_each = toset(local.generated_keys)

  project   = var.project_id
  secret_id = local.secret_ids[each.key]
  labels    = local.labels

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "generated" {
  for_each = toset(local.generated_keys)

  secret      = google_secret_manager_secret.generated[each.key].id
  secret_data = local.secret_values[each.key]
}

resource "google_secret_manager_secret" "shell" {
  for_each = toset(local.shell_keys)

  project   = var.project_id
  secret_id = local.secret_ids[each.key]
  labels    = local.labels

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

# Only when a value was supplied. Otherwise the secret stays empty until you add a version.
resource "google_secret_manager_secret_version" "shell" {
  for_each = toset([for k in local.shell_keys : k if local.shell_has_value[k]])

  secret      = google_secret_manager_secret.shell[each.key].id
  secret_data = local.shell_values[each.key]
}
