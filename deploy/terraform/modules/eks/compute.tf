# --- Managed node group --------------------------------------------------------

data "aws_iam_policy_document" "node_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "node" {
  count = var.create_cluster && !local.fargate ? 1 : 0

  name               = "${var.name}-eks-node"
  assume_role_policy = data.aws_iam_policy_document.node_assume.json
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "node" {
  for_each = var.create_cluster && !local.fargate ? toset([
    "AmazonEKSWorkerNodePolicy",
    "AmazonEKS_CNI_Policy",
    "AmazonEC2ContainerRegistryReadOnly",
    "AmazonSSMManagedInstanceCore",
  ]) : toset([])

  role       = aws_iam_role.node[0].name
  policy_arn = "arn:${local.partition}:iam::aws:policy/${each.value}"
}

resource "aws_eks_node_group" "this" {
  count = var.create_cluster && !local.fargate ? 1 : 0

  cluster_name    = local.cluster_name
  node_group_name = "${var.name}-default"
  node_role_arn   = aws_iam_role.node[0].arn
  subnet_ids      = var.private_subnet_ids
  ami_type        = "AL2023_x86_64_STANDARD"
  instance_types  = var.node_instance_types
  capacity_type   = var.node_capacity_type
  disk_size       = 50
  tags            = local.tags

  scaling_config {
    min_size     = var.node_min_size
    max_size     = var.node_max_size
    desired_size = var.node_desired_size
  }

  update_config {
    max_unavailable = 1
  }

  # The desired size moves with the cluster autoscaler / Karpenter if you add one.
  lifecycle {
    ignore_changes = [scaling_config[0].desired_size]
  }

  depends_on = [aws_iam_role_policy_attachment.node]
}

# --- Fargate -------------------------------------------------------------------

data "aws_iam_policy_document" "fargate_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["eks-fargate-pods.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "fargate" {
  count = var.create_cluster && local.fargate ? 1 : 0

  name               = "${var.name}-eks-fargate"
  assume_role_policy = data.aws_iam_policy_document.fargate_assume.json
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "fargate" {
  count = var.create_cluster && local.fargate ? 1 : 0

  role       = aws_iam_role.fargate[0].name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AmazonEKSFargatePodExecutionRolePolicy"
}

resource "aws_eks_fargate_profile" "this" {
  for_each = var.create_cluster && local.fargate ? toset([var.namespace, "kube-system"]) : toset([])

  cluster_name           = local.cluster_name
  fargate_profile_name   = "${var.name}-${each.value}"
  pod_execution_role_arn = aws_iam_role.fargate[0].arn
  subnet_ids             = var.private_subnet_ids
  tags                   = local.tags

  selector {
    namespace = each.value
  }

  depends_on = [aws_iam_role_policy_attachment.fargate]
}
