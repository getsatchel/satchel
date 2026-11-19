# The pods' IAM roles, one per Kubernetes service account. With Pod Identity (the default) a role
# trusts pods.eks.amazonaws.com and an association binds it to its service account; with IRSA it
# trusts the cluster's OIDC provider for exactly that service account. The resources keep their
# "irsa" names so existing state does not move. The service accounts:
#   <ns>/satchel          runtime (web, worker, first-workspace Job)
#   <ns>/satchel-migrate  the pre-upgrade migrate Job (owner-level secrets)
#   kube-system/aws-load-balancer-controller
locals {
  workload_roles = merge(
    {
      runtime = { sa = "${var.namespace}:satchel", policy = var.runtime_policy_arn }
      migrate = { sa = "${var.namespace}:satchel-migrate", policy = var.migrate_policy_arn }
    },
    var.install_cluster_addons ? {
      lbc = { sa = "kube-system:aws-load-balancer-controller", policy = aws_iam_policy.lbc[0].arn }
    } : {},
  )
}

data "aws_iam_policy_document" "irsa_assume" {
  for_each = local.irsa ? local.workload_roles : {}

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:sub"
      values   = ["system:serviceaccount:${each.value.sa}"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "pod_identity_assume" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.this.account_id]
    }
  }
}

data "aws_caller_identity" "this" {}

resource "aws_iam_role" "irsa" {
  for_each = local.workload_roles

  name = "${var.name}-${each.key}"
  assume_role_policy = (local.irsa ? data.aws_iam_policy_document.irsa_assume[each.key].json
  : data.aws_iam_policy_document.pod_identity_assume.json)
  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "irsa" {
  for_each = local.workload_roles

  role       = aws_iam_role.irsa[each.key].name
  policy_arn = each.value.policy
}

resource "aws_eks_pod_identity_association" "this" {
  for_each = local.irsa ? {} : local.workload_roles

  cluster_name    = local.cluster_name
  namespace       = split(":", each.value.sa)[0]
  service_account = split(":", each.value.sa)[1]
  role_arn        = aws_iam_role.irsa[each.key].arn
  tags            = local.tags

  depends_on = [aws_eks_addon.pod_identity_agent]
}

# Upstream policy for the AWS Load Balancer Controller, vendored from
# kubernetes-sigs/aws-load-balancer-controller docs/install/iam_policy.json (v3.6.2).
resource "aws_iam_policy" "lbc" {
  count = var.install_cluster_addons ? 1 : 0

  name   = "${var.name}-aws-load-balancer-controller"
  policy = file("${path.module}/policies/aws-load-balancer-controller.json")
  tags   = local.tags
}
