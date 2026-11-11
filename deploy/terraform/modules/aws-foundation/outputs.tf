output "account_id" {
  value = local.account_id
}

output "region" {
  value = local.region
}

output "partition" {
  value = local.partition
}

output "vpc_id" {
  value = local.vpc_id
}

output "vpc_cidr" {
  description = "CIDR of the created VPC (null for an existing VPC)."
  value       = var.create_vpc ? aws_vpc.this[0].cidr_block : null
}

output "private_subnet_ids" {
  value = local.private_subnet_ids
}

output "public_subnet_ids" {
  value = local.public_subnet_ids
}

output "kms_key_arn" {
  value = local.kms_key_arn
}

output "database_endpoint" {
  description = "RDS address:port."
  value       = "${aws_db_instance.this.address}:${aws_db_instance.this.port}"
}

output "database_identifier" {
  value = aws_db_instance.this.identifier
}

output "database_security_group_id" {
  description = "Security group on RDS; the EKS module adds an ingress rule from the cluster."
  value       = aws_security_group.database.id
}

output "secret_names" {
  description = "Secrets Manager names by role (for example database_url = satchel/database-url)."
  value       = local.secret_names
}

output "secret_arns" {
  value = merge(
    { for k, s in aws_secretsmanager_secret.generated : k => s.arn },
    { for k, s in aws_secretsmanager_secret.shell : k => s.arn },
  )
}

output "owner_secret_keys" {
  description = "Keys of secret_names that only the migrate identity may read."
  value       = local.owner_secret_keys
}

output "database_url_secrets" {
  description = "Sensitive DB URL values (for Kubernetes Secrets when the CSI driver is not used)."
  value       = local.database_urls
  sensitive   = true
}

output "encryption_key" {
  description = "SATCHEL_ENCRYPTION_KEY (for Kubernetes Secrets when the CSI driver is not used)."
  value       = random_id.encryption_key.hex
  sensitive   = true
}

output "database_passwords" {
  description = "Generated role passwords (bootstrap only)."
  value = {
    app    = random_password.app.result
    worker = random_password.worker.result
  }
  sensitive = true
}

output "results_bucket" {
  value = aws_s3_bucket.results.bucket
}

output "audit_bucket" {
  value = aws_s3_bucket.audit.bucket
}

output "runtime_policy_arn" {
  value = aws_iam_policy.runtime.arn
}

output "migrate_policy_arn" {
  value = aws_iam_policy.migrate.arn
}
