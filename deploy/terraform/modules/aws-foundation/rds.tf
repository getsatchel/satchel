# RDS for PostgreSQL 17. pgvector needs no parameter: it is an ordinary extension
# (`CREATE EXTENSION vector`) that `satchel db bootstrap` creates as the master user, who holds
# rds_superuser. TLS is enforced server side (rds.force_ssl).
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

resource "random_id" "db_suffix" {
  byte_length = 2
}

resource "aws_db_subnet_group" "this" {
  name       = "${var.name}-${random_id.db_suffix.hex}"
  subnet_ids = local.private_subnet_ids
  tags       = local.tags
}

resource "aws_security_group" "database" {
  name        = "${var.name}-database"
  description = "satchel Postgres: 5432 from the EKS cluster and the listed sources only"
  vpc_id      = local.vpc_id
  tags        = merge(local.tags, { Name = "${var.name}-database" })
}

resource "aws_vpc_security_group_ingress_rule" "database_sg" {
  for_each = toset(var.database_allowed_security_group_ids)

  security_group_id            = aws_security_group.database.id
  referenced_security_group_id = each.value
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_vpc_security_group_ingress_rule" "database_cidr" {
  for_each = toset(var.database_allowed_cidrs)

  security_group_id = aws_security_group.database.id
  cidr_ipv4         = each.value
  ip_protocol       = "tcp"
  from_port         = 5432
  to_port           = 5432
}

resource "aws_db_parameter_group" "this" {
  name   = "${var.name}-pg${split(".", var.database_engine_version)[0]}-${random_id.db_suffix.hex}"
  family = "postgres${split(".", var.database_engine_version)[0]}"
  tags   = local.tags

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  parameter {
    name  = "log_min_duration_statement"
    value = "1000"
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_db_instance" "this" {
  identifier     = "${var.name}-${random_id.db_suffix.hex}"
  engine         = "postgres"
  engine_version = var.database_engine_version
  instance_class = var.database_instance_class

  db_name  = "satchel"
  username = "satchel"
  password = random_password.owner.result
  port     = 5432

  allocated_storage     = var.database_allocated_storage_gb
  max_allocated_storage = var.database_max_allocated_storage_gb == 0 ? null : var.database_max_allocated_storage_gb
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = local.kms_key_arn

  multi_az               = var.database_multi_az
  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.database.id]
  publicly_accessible    = false
  parameter_group_name   = aws_db_parameter_group.this.name

  backup_retention_period  = var.database_backup_retention_days
  backup_window            = var.database_backup_window
  maintenance_window       = var.database_maintenance_window
  copy_tags_to_snapshot    = true
  delete_automated_backups = false

  auto_minor_version_upgrade = true
  apply_immediately          = false

  performance_insights_enabled    = var.database_performance_insights
  performance_insights_kms_key_id = var.database_performance_insights ? local.kms_key_arn : null
  enabled_cloudwatch_logs_exports = ["postgresql"]

  deletion_protection       = var.database_deletion_protection
  skip_final_snapshot       = !var.database_deletion_protection
  final_snapshot_identifier = var.database_deletion_protection ? "${var.name}-${random_id.db_suffix.hex}-final" : null

  tags = local.tags
}
