# SATCHEL_ENCRYPTION_KEY: AES-256-GCM key for secrets satchel stores in Postgres (LLM API keys
# added in Settings → Models). 256 bits as 64 hex characters. Kept stable across applies; to
# rotate, follow docs/deploy/configuration.md (taint this resource, keep the old value in
# SATCHEL_ENCRYPTION_KEY_PREVIOUS, run `satchel secrets rotate`).
resource "random_id" "encryption_key" {
  byte_length = 32
}

locals {
  db_host_part = coalesce(var.database_host_override, "${aws_db_instance.this.address}:${aws_db_instance.this.port}")
  db_query     = "?sslmode=${var.database_sslmode}"

  database_urls = {
    # satchel_app: request traffic, row-level security applies.
    database_url = "postgresql+psycopg://satchel_app:${random_password.app.result}@${local.db_host_part}/satchel${local.db_query}"
    # satchel_worker: server-internal jobs.
    admin_database_url = "postgresql+psycopg://satchel_worker:${random_password.worker.result}@${local.db_host_part}/satchel${local.db_query}"
    # owner (RDS master user): migrations. Also the bootstrap URL.
    migration_database_url = "postgresql+psycopg://satchel:${random_password.owner.result}@${local.db_host_part}/satchel${local.db_query}"
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

  # Secrets created empty; a value is set only when one was supplied (otherwise put-secret-value).
  shell_keys = concat(["oidc_client_secret"], var.enable_chat_api_key ? ["chat_api_key"] : [])
  shell_values = {
    oidc_client_secret = var.oidc_client_secret
    chat_api_key       = var.chat_api_key
  }
  shell_has_value = {
    oidc_client_secret = nonsensitive(var.oidc_client_secret != null)
    chat_api_key       = nonsensitive(var.chat_api_key != null)
  }

  # Secrets Manager names: <name>/database-url, <name>/oidc-client-secret, ...
  secret_names = {
    for k in concat(local.generated_keys, local.shell_keys) :
    k => "${var.name}/${replace(k, "_", "-")}"
  }

  # Owner-level secrets: readable by the migrate role only, never by web or worker pods.
  owner_secret_keys = ["owner_password", "migration_database_url", "app_password", "worker_password"]
}

resource "aws_secretsmanager_secret" "generated" {
  for_each = toset(local.generated_keys)

  name                    = local.secret_names[each.key]
  kms_key_id              = local.kms_key_arn
  recovery_window_in_days = var.secret_recovery_window_days
  tags                    = merge(local.tags, { satchel-scope = contains(local.owner_secret_keys, each.key) ? "owner" : "runtime" })
}

resource "aws_secretsmanager_secret_version" "generated" {
  for_each = toset(local.generated_keys)

  secret_id     = aws_secretsmanager_secret.generated[each.key].id
  secret_string = local.secret_values[each.key]
}

resource "aws_secretsmanager_secret" "shell" {
  for_each = toset(local.shell_keys)

  name                    = local.secret_names[each.key]
  kms_key_id              = local.kms_key_arn
  recovery_window_in_days = var.secret_recovery_window_days
  tags                    = merge(local.tags, { satchel-scope = "runtime" })
}

# Only when a value was supplied. Otherwise the secret has no value until you put one.
resource "aws_secretsmanager_secret_version" "shell" {
  for_each = toset([for k in local.shell_keys : k if local.shell_has_value[k]])

  secret_id     = aws_secretsmanager_secret.shell[each.key].id
  secret_string = local.shell_values[each.key]
}
