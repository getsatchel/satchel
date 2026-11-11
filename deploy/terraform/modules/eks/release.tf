resource "kubernetes_namespace_v1" "this" {
  metadata {
    name = var.namespace
    labels = {
      "app.kubernetes.io/part-of" = "satchel"
    }
  }

  depends_on = [aws_eks_node_group.this, aws_eks_fargate_profile.this]
}

# --- Cluster add-ons (Helm) ------------------------------------------------------

resource "helm_release" "load_balancer_controller" {
  count = var.install_cluster_addons ? 1 : 0

  name       = "aws-load-balancer-controller"
  namespace  = "kube-system"
  repository = "https://aws.github.io/eks-charts"
  chart      = "aws-load-balancer-controller"
  version    = var.load_balancer_controller_chart_version
  atomic     = true
  timeout    = 600

  values = [yamlencode({
    clusterName  = local.cluster_name
    region       = local.region
    vpcId        = var.vpc_id
    replicaCount = 2
    serviceAccount = {
      create = true
      name   = "aws-load-balancer-controller"
      annotations = {
        "eks.amazonaws.com/role-arn" = aws_iam_role.irsa["lbc"].arn
      }
    }
  })]

  depends_on = [aws_iam_role_policy_attachment.irsa, aws_eks_addon.coredns, aws_eks_node_group.this, aws_eks_fargate_profile.this]
}

# The upstream Secrets Store CSI driver (a dependency of this chart) plus the AWS provider. The
# driver syncs mounted secrets into the Kubernetes Secrets the chart's secretKeyRefs read.
resource "helm_release" "secrets_store_csi" {
  count = var.install_cluster_addons && local.secrets_delivery == "csi" ? 1 : 0

  name       = "secrets-provider-aws"
  namespace  = "kube-system"
  repository = "https://aws.github.io/secrets-store-csi-driver-provider-aws"
  chart      = "secrets-store-csi-driver-provider-aws"
  version    = var.secrets_provider_chart_version
  atomic     = true
  timeout    = 600

  values = [yamlencode({
    "secrets-store-csi-driver" = {
      install    = true
      syncSecret = { enabled = true }
      # Picks up put-secret-value (for example the OIDC secret) without restarting pods.
      enableSecretRotation = true
      rotationPollInterval = "2m"
    }
  })]

  depends_on = [aws_eks_node_group.this]
}

# --- Kubernetes Secrets (Fargate / secrets_delivery = kubernetes) ----------------

resource "kubernetes_secret_v1" "satchel" {
  count = local.secrets_delivery == "kubernetes" ? 1 : 0

  metadata {
    name      = "${var.name}-secrets"
    namespace = kubernetes_namespace_v1.this.metadata[0].name
  }

  data = merge(
    {
      SATCHEL_DATABASE_URL           = var.database_urls["database_url"]
      SATCHEL_ADMIN_DATABASE_URL     = var.database_urls["admin_database_url"]
      SATCHEL_MIGRATION_DATABASE_URL = var.database_urls["migration_database_url"]
    },
    var.oidc_client_secret == null ? {} : { SATCHEL_OIDC_CLIENT_SECRET = var.oidc_client_secret },
    var.chat_api_key == null ? {} : { SATCHEL_CHAT_API_KEY = var.chat_api_key },
    var.encryption_key == null ? {} : { SATCHEL_ENCRYPTION_KEY = var.encryption_key },
  )

  lifecycle {
    precondition {
      condition     = var.oidc_client_secret != null
      error_message = "oidc_client_secret is required when secrets are delivered as Kubernetes Secrets (compute = fargate or secrets_delivery = kubernetes): there is no CSI driver to read it from Secrets Manager later."
    }
  }
}

resource "kubernetes_secret_v1" "bootstrap" {
  count = local.secrets_delivery == "kubernetes" ? 1 : 0

  metadata {
    name      = "${var.name}-bootstrap"
    namespace = kubernetes_namespace_v1.this.metadata[0].name
  }

  data = {
    SATCHEL_BOOTSTRAP_DATABASE_URL = var.database_urls["migration_database_url"]
    SATCHEL_APP_DB_PASSWORD        = var.database_passwords["app"]
    SATCHEL_WORKER_DB_PASSWORD     = var.database_passwords["worker"]
  }
}

# --- satchel -----------------------------------------------------------------------

module "values" {
  source = "../aws-helm-values"

  image_repository       = var.image_repository
  image_tag              = var.image_tag
  public_url             = local.public_url
  domain                 = var.domain
  region                 = local.region
  results_bucket         = var.results_bucket
  runtime_role_arn       = aws_iam_role.irsa["runtime"].arn
  migrate_role_arn       = aws_iam_role.irsa["migrate"].arn
  secrets_delivery       = local.secrets_delivery
  secret_names           = var.secret_names
  existing_secret_name   = try(kubernetes_secret_v1.satchel[0].metadata[0].name, null)
  bootstrap_secret_name  = try(kubernetes_secret_v1.bootstrap[0].metadata[0].name, null)
  bootstrap_enabled      = var.bootstrap_enabled
  oidc_issuer            = var.oidc_issuer
  oidc_client_id         = var.oidc_client_id
  chat_provider          = var.chat_provider
  chat_model             = var.chat_model
  session_retention_days = var.session_retention_days
  replicas               = var.replicas
  alb_scheme             = var.alb_scheme
  certificate_arn        = local.certificate_arn
  waf_acl_arn            = local.waf_acl_arn
  # VPC CNI does not enforce NetworkPolicy on Fargate.
  network_policy_cidrs = var.vpc_cidr == null || local.fargate ? [] : [var.vpc_cidr]
  first_workspace      = var.first_workspace
  extra_values         = var.extra_values
}

resource "helm_release" "satchel" {
  name             = var.name
  namespace        = kubernetes_namespace_v1.this.metadata[0].name
  create_namespace = false
  chart            = local.chart_path
  atomic           = true
  timeout          = var.helm_timeout

  values = [module.values.values_yaml]

  depends_on = [
    aws_iam_role_policy_attachment.irsa,
    aws_vpc_security_group_ingress_rule.database_from_cluster,
    helm_release.load_balancer_controller,
    helm_release.secrets_store_csi,
  ]
}
