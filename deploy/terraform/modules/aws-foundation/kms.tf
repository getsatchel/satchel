# One customer managed key for satchel's data at rest: RDS storage, Secrets Manager, S3 objects
# and (via the EKS module) Kubernetes Secrets envelope encryption. Rotated yearly.
resource "aws_kms_key" "this" {
  count = var.kms_key_arn == null ? 1 : 0

  description             = "satchel (${var.name}) data at rest"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  tags                    = local.tags
}

resource "aws_kms_alias" "this" {
  count = var.kms_key_arn == null ? 1 : 0

  name          = "alias/${var.name}"
  target_key_id = aws_kms_key.this[0].key_id
}

locals {
  kms_key_arn = var.kms_key_arn == null ? aws_kms_key.this[0].arn : var.kms_key_arn
}
