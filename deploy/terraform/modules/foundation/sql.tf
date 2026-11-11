resource "random_id" "instance_suffix" {
  byte_length = 2
}

# Cloud SQL for PostgreSQL 17. pgvector needs NO database flag on Cloud SQL: it is an
# ordinary extension (`CREATE EXTENSION vector`) that `satchel db bootstrap` creates, and the
# owner user below is a member of cloudsqlsuperuser, which is allowed to do so.
resource "google_sql_database_instance" "this" {
  project             = var.project_id
  name                = "${var.name}-${random_id.instance_suffix.hex}"
  region              = var.region
  database_version    = "POSTGRES_17"
  deletion_protection = var.database_deletion_protection

  settings {
    tier                        = var.database_tier
    edition                     = var.database_edition
    availability_type           = var.database_availability_type
    disk_type                   = "PD_SSD"
    disk_size                   = var.database_disk_size_gb
    disk_autoresize             = true
    deletion_protection_enabled = var.database_deletion_protection
    user_labels                 = local.labels

    ip_configuration {
      # No authorized_networks block, ever: public IP (when enabled) is only usable through
      # the IAM-authenticated Cloud SQL connector.
      ipv4_enabled    = !var.database_private_ip
      private_network = var.database_private_ip ? local.network_id : null
      ssl_mode        = "ENCRYPTED_ONLY"
    }

    backup_configuration {
      enabled                        = true
      start_time                     = var.database_backup_start_time
      point_in_time_recovery_enabled = true
      transaction_log_retention_days = var.database_pitr_log_retention_days

      backup_retention_settings {
        retained_backups = var.database_backup_retention_count
        retention_unit   = "COUNT"
      }
    }

    maintenance_window {
      day          = var.database_maintenance_day
      hour         = var.database_maintenance_hour
      update_track = "stable"
    }

    insights_config {
      query_insights_enabled = true
    }
  }

  depends_on = [
    google_project_service.apis,
    google_service_networking_connection.private_service_access,
  ]
}

resource "google_sql_database" "satchel" {
  count = var.create_database ? 1 : 0

  project  = var.project_id
  instance = google_sql_database_instance.this.name
  name     = "satchel"
}

# create_database gained a count; keep existing state (and the database) in place.
moved {
  from = google_sql_database.satchel
  to   = google_sql_database.satchel[0]
}

# The owner. It owns every table and runs `satchel db bootstrap` and `satchel db upgrade`.
resource "google_sql_user" "owner" {
  project  = var.project_id
  instance = google_sql_database_instance.this.name
  name     = "satchel"
  password = random_password.owner.result
}
