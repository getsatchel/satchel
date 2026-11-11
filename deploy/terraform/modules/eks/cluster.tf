data "aws_partition" "this" {}
data "aws_region" "this" {}

locals {
  tags       = merge({ app = "satchel", managed-by = "terraform" }, var.tags)
  partition  = data.aws_partition.this.partition
  region     = data.aws_region.this.region
  chart_path = coalesce(var.chart_path, "${path.module}/../../../helm/satchel")
  public_url = "https://${var.domain}"
  # The chart's fullname: the release name when it already contains "satchel".
  release_fullname = strcontains(var.name, "satchel") ? var.name : "${var.name}-satchel"
  fargate          = var.compute == "fargate"
  # The CSI driver is a DaemonSet; Fargate has no DaemonSets, so it gets Kubernetes Secrets.
  secrets_delivery = local.fargate ? "kubernetes" : var.secrets_delivery

  cluster_name = var.create_cluster ? aws_eks_cluster.this[0].name : data.aws_eks_cluster.existing[0].name
  cluster      = var.create_cluster ? aws_eks_cluster.this[0] : data.aws_eks_cluster.existing[0]
  oidc_issuer  = local.cluster.identity[0].oidc[0].issuer
  oidc_host    = replace(local.oidc_issuer, "https://", "")
  oidc_provider_arn = coalesce(
    var.oidc_provider_arn,
    try(aws_iam_openid_connect_provider.this[0].arn, null),
    try(data.aws_iam_openid_connect_provider.existing[0].arn, null),
  )
}

data "aws_eks_cluster" "existing" {
  count = var.create_cluster ? 0 : 1
  name  = var.existing_cluster_name
}

data "aws_iam_openid_connect_provider" "existing" {
  count = !var.create_cluster && var.oidc_provider_arn == null ? 1 : 0
  url   = data.aws_eks_cluster.existing[0].identity[0].oidc[0].issuer
}

# --- Control plane -----------------------------------------------------------

data "aws_iam_policy_document" "cluster_assume" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "cluster" {
  count = var.create_cluster ? 1 : 0

  name               = "${var.name}-eks-cluster"
  assume_role_policy = data.aws_iam_policy_document.cluster_assume.json
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "cluster" {
  count = var.create_cluster ? 1 : 0

  role       = aws_iam_role.cluster[0].name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AmazonEKSClusterPolicy"
}

resource "aws_cloudwatch_log_group" "cluster" {
  count = var.create_cluster ? 1 : 0

  # EKS writes control plane logs here; created first so retention applies.
  name              = "/aws/eks/${var.name}/cluster"
  retention_in_days = 90
  tags              = local.tags
}

resource "aws_eks_cluster" "this" {
  count = var.create_cluster ? 1 : 0

  name                      = var.name
  version                   = var.kubernetes_version
  role_arn                  = aws_iam_role.cluster[0].arn
  enabled_cluster_log_types = ["api", "audit", "authenticator"]
  deletion_protection       = var.cluster_deletion_protection
  tags                      = local.tags

  vpc_config {
    subnet_ids              = var.private_subnet_ids
    endpoint_private_access = true
    endpoint_public_access  = var.endpoint_public_access
    public_access_cidrs     = var.endpoint_public_access ? var.public_access_cidrs : null
  }

  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = true
  }

  # Kubernetes Secrets are envelope-encrypted with the foundation key.
  encryption_config {
    resources = ["secrets"]
    provider {
      key_arn = var.kms_key_arn
    }
  }

  depends_on = [
    aws_iam_role_policy_attachment.cluster,
    aws_cloudwatch_log_group.cluster,
  ]
}

resource "aws_eks_access_entry" "admins" {
  for_each = var.create_cluster ? toset(var.cluster_admin_principal_arns) : toset([])

  cluster_name  = local.cluster_name
  principal_arn = each.value
  tags          = local.tags
}

resource "aws_eks_access_policy_association" "admins" {
  for_each = aws_eks_access_entry.admins

  cluster_name  = local.cluster_name
  principal_arn = each.value.principal_arn
  policy_arn    = "arn:${local.partition}:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"

  access_scope {
    type = "cluster"
  }
}

# IRSA: pods exchange their projected service account token for role credentials.
resource "aws_iam_openid_connect_provider" "this" {
  count = var.create_cluster && var.oidc_provider_arn == null ? 1 : 0

  url            = aws_eks_cluster.this[0].identity[0].oidc[0].issuer
  client_id_list = ["sts.amazonaws.com"]
  tags           = local.tags
}

# Pods (managed nodes and Fargate both use the cluster security group) may reach RDS.
resource "aws_vpc_security_group_ingress_rule" "database_from_cluster" {
  security_group_id            = var.database_security_group_id
  referenced_security_group_id = local.cluster.vpc_config[0].cluster_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  description                  = "satchel pods on EKS"
}

# --- Add-ons -----------------------------------------------------------------

resource "aws_eks_addon" "vpc_cni" {
  count = var.create_cluster ? 1 : 0

  cluster_name = local.cluster_name
  addon_name   = "vpc-cni"
  # Network policy enforcement for the chart's NetworkPolicy (managed nodes only).
  configuration_values        = jsonencode({ enableNetworkPolicy = "true" })
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"
  tags                        = local.tags
}

resource "aws_eks_addon" "kube_proxy" {
  count = var.create_cluster ? 1 : 0

  cluster_name                = local.cluster_name
  addon_name                  = "kube-proxy"
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"
  tags                        = local.tags
}

resource "aws_eks_addon" "coredns" {
  count = var.create_cluster ? 1 : 0

  cluster_name                = local.cluster_name
  addon_name                  = "coredns"
  configuration_values        = local.fargate ? jsonencode({ computeType = "Fargate" }) : null
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"
  tags                        = local.tags

  # CoreDNS needs somewhere to run.
  depends_on = [aws_eks_node_group.this, aws_eks_fargate_profile.this]
}
