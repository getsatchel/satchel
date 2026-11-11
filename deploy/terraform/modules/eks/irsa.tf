# IAM Roles for Service Accounts. Each role trusts exactly one Kubernetes service account:
#   <ns>/satchel          runtime (web, worker, first-workspace Job)
#   <ns>/satchel-migrate  the pre-upgrade migrate Job (owner-level secrets)
#   kube-system/aws-load-balancer-controller
locals {
  irsa = merge(
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
  for_each = local.irsa

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

resource "aws_iam_role" "irsa" {
  for_each = local.irsa

  name               = "${var.name}-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.irsa_assume[each.key].json
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "irsa" {
  for_each = local.irsa

  role       = aws_iam_role.irsa[each.key].name
  policy_arn = each.value.policy
}

# Upstream policy for the AWS Load Balancer Controller, vendored from
# kubernetes-sigs/aws-load-balancer-controller docs/install/iam_policy.json (v3.6.2).
resource "aws_iam_policy" "lbc" {
  count = var.install_cluster_addons ? 1 : 0

  name   = "${var.name}-aws-load-balancer-controller"
  policy = file("${path.module}/policies/aws-load-balancer-controller.json")
  tags   = local.tags
}
