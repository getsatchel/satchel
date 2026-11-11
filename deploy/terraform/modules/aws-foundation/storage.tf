# Two private buckets. results: disposable Parquet result cache, expired by lifecycle a day after
# satchel's own TTL. audit: versioned JSONL exports, optionally under S3 Object Lock.
resource "aws_s3_bucket" "results" {
  bucket        = "${local.bucket_prefix}-results"
  force_destroy = var.results_bucket_force_destroy
  tags          = local.tags
}

resource "aws_s3_bucket" "audit" {
  bucket              = "${local.bucket_prefix}-audit"
  force_destroy       = var.audit_bucket_force_destroy
  object_lock_enabled = var.audit_retention_days != null
  tags                = local.tags
}

locals {
  buckets = {
    results = aws_s3_bucket.results
    audit   = aws_s3_bucket.audit
  }
}

resource "aws_s3_bucket_public_access_block" "this" {
  for_each = local.buckets

  bucket                  = each.value.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "this" {
  for_each = local.buckets

  bucket = each.value.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  for_each = local.buckets

  bucket = each.value.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = var.s3_kms_encryption ? "aws:kms" : "AES256"
      kms_master_key_id = var.s3_kms_encryption ? local.kms_key_arn : null
    }
    bucket_key_enabled = var.s3_kms_encryption
  }
}

# TLS only, for both buckets.
data "aws_iam_policy_document" "tls_only" {
  for_each = local.buckets

  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      each.value.arn,
      "${each.value.arn}/*",
    ]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "this" {
  for_each = local.buckets

  bucket = each.value.id
  policy = data.aws_iam_policy_document.tls_only[each.key].json

  depends_on = [aws_s3_bucket_public_access_block.this]
}

resource "aws_s3_bucket_lifecycle_configuration" "results" {
  bucket = aws_s3_bucket.results.id

  rule {
    id     = "expire-results"
    status = "Enabled"
    filter {}
    expiration {
      days = var.result_ttl_days + 1
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

resource "aws_s3_bucket_versioning" "audit" {
  bucket = aws_s3_bucket.audit.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_object_lock_configuration" "audit" {
  count = var.audit_retention_days == null ? 0 : 1

  bucket = aws_s3_bucket.audit.id
  rule {
    default_retention {
      mode = var.audit_retention_mode
      days = var.audit_retention_days
    }
  }

  depends_on = [aws_s3_bucket_versioning.audit]
}
