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

  statement {
    sid       = "Kms"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = [local.kms_key_arn]
  }
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
}

resource "aws_iam_policy" "migrate" {
  name        = "${var.name}-migrate"
  description = "satchel migrate job: every satchel secret, including owner-level ones"
  policy      = data.aws_iam_policy_document.migrate.json
  tags        = local.tags
}
