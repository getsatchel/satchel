# Least-privilege permission policies for the two identities. The compute module (eks) creates
# the roles and their trust policies (IRSA); these documents say what each may do.
#
# runtime (web, worker, first-workspace job): read/write/delete result objects, write audit
# exports, read the runtime secrets, use the KMS key for those.
# migrate (the pre-upgrade migrate Job only): read every satchel secret, including the owner's.
data "aws_iam_policy_document" "runtime" {
  statement {
    sid       = "ResultsObjects"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.results.arn}/*"]
  }

  statement {
    sid       = "ResultsList"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.results.arn]
  }

  # Audit export rewrites whole-day objects (versioned bucket), so PutObject is enough.
  statement {
    sid       = "AuditWrite"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.audit.arn}/*"]
  }

  # Source artifacts (s3:// locations on sources): read-only.
  dynamic "statement" {
    for_each = length(var.source_artifact_buckets) > 0 ? [1] : []
    content {
      sid       = "SourceArtifactsRead"
      actions   = ["s3:GetObject"]
      resources = [for b in var.source_artifact_buckets : "arn:${data.aws_partition.this.partition}:s3:::${b}/*"]
    }
  }

  dynamic "statement" {
    for_each = length(var.source_artifact_buckets) > 0 ? [1] : []
    content {
      sid       = "SourceArtifactsList"
      actions   = ["s3:ListBucket"]
      resources = [for b in var.source_artifact_buckets : "arn:${data.aws_partition.this.partition}:s3:::${b}"]
    }
  }

  statement {
    sid     = "RuntimeSecrets"
    actions = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = concat(
      [for k, s in aws_secretsmanager_secret.generated : s.arn if !contains(local.owner_secret_keys, k)],
      [for s in aws_secretsmanager_secret.shell : s.arn],
    )
  }

  # Claude on Amazon Bedrock with the runtime role (provider "bedrock"): no API key stored.
  dynamic "statement" {
    for_each = var.enable_bedrock ? [1] : []
    content {
      sid     = "BedrockInvoke"
      actions = ["bedrock:InvokeModel"]
      resources = [
        "arn:${local.partition}:bedrock:*::foundation-model/*",
        "arn:${local.partition}:bedrock:*:${local.account_id}:inference-profile/*",
      ]
    }
  }

  # Secrets that connections, models and sources reference (aws-sm:...) instead of storing.
  dynamic "statement" {
    for_each = var.secret_ref_prefix == null ? [] : [1]
    content {
      sid       = "ReferencedSecrets"
      actions   = ["secretsmanager:GetSecretValue"]
      resources = [local.secret_ref_arn]
    }
  }

  statement {
    sid       = "Kms"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = [local.kms_key_arn]
  }
}

locals {
  secret_ref_arn = var.secret_ref_prefix == null ? null : "arn:${local.partition}:secretsmanager:${local.region}:${local.account_id}:secret:${var.secret_ref_prefix}*"
  # Both forms the server accepts: the full ARN, and the bare name (read in this region).
  secret_ref_allow = var.secret_ref_prefix == null ? [] : [
    "aws-sm:${local.secret_ref_arn}",
    "aws-sm:${var.secret_ref_prefix}*",
  ]
}

data "aws_iam_policy_document" "migrate" {
  statement {
    sid     = "AllSecrets"
    actions = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = concat(
      [for s in aws_secretsmanager_secret.generated : s.arn],
      [for s in aws_secretsmanager_secret.shell : s.arn],
    )
  }

  statement {
    sid       = "Kms"
    actions   = ["kms:Decrypt", "kms:DescribeKey"]
    resources = [local.kms_key_arn]
  }
}

resource "aws_iam_policy" "runtime" {
  name        = "${var.name}-runtime"
  description = "satchel runtime: results bucket, audit writes, runtime secrets"
  policy      = data.aws_iam_policy_document.runtime.json
  tags        = local.tags

  lifecycle {
    # A prefix that covers the module's own secrets would let the runtime read the owner-level
    # ones (migrate role only) through a reference.
    precondition {
      condition     = var.secret_ref_prefix == null ? true : alltrue([for n in values(local.secret_names) : !startswith(n, var.secret_ref_prefix)])
      error_message = "secret_ref_prefix must not cover satchel's own secrets (${var.name}/...): use a separate path such as \"${var.name}/refs/\"."
    }
  }
}

resource "aws_iam_policy" "migrate" {
  name        = "${var.name}-migrate"
  description = "satchel migrate job: every satchel secret, including owner-level ones"
  policy      = data.aws_iam_policy_document.migrate.json
  tags        = local.tags
}
