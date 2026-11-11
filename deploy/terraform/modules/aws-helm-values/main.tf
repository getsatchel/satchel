# Pure function from inputs to the chart's values for EKS, kept free of resources so CI can render
# it and run `helm lint --strict` / `helm template` against deploy/helm/satchel
# (scripts/check-helm-values.sh). Keys mirror deploy/helm/satchel/values.yaml (strict schema).
locals {
  csi = var.secrets_delivery == "csi"

  # Chart Secret key (env var name) -> aws-foundation secret key.
  runtime_secret_keys = merge(
    {
      SATCHEL_DATABASE_URL       = "database_url"
      SATCHEL_ADMIN_DATABASE_URL = "admin_database_url"
      SATCHEL_OIDC_CLIENT_SECRET = "oidc_client_secret"
    },
    contains(keys(var.secret_names), "chat_api_key") ? { SATCHEL_CHAT_API_KEY = "chat_api_key" } : {},
    contains(keys(var.secret_names), "encryption_key") ? { SATCHEL_ENCRYPTION_KEY = "encryption_key" } : {},
  )
  bootstrap_secret_keys = {
    SATCHEL_BOOTSTRAP_DATABASE_URL = "migration_database_url"
    SATCHEL_APP_DB_PASSWORD        = "app_password"
    SATCHEL_WORKER_DB_PASSWORD     = "worker_password"
  }

  csi_values = {
    existingSecret = ""
    secretManager = {
      enabled          = true
      provider         = "aws"
      secrets          = { for env, k in local.runtime_secret_keys : env => lookup(var.secret_names, k, "") }
      migrationSecrets = { SATCHEL_MIGRATION_DATABASE_URL = lookup(var.secret_names, "migration_database_url", "") }
      bootstrapSecrets = { for env, k in local.bootstrap_secret_keys : env => lookup(var.secret_names, k, "") }
    }
    bootstrap = {
      enabled = var.bootstrap_enabled
    }
  }
  kubernetes_values = {
    existingSecret = var.existing_secret_name
    bootstrap = {
      enabled        = var.bootstrap_enabled
      existingSecret = var.bootstrap_secret_name
    }
  }

  values = merge(
    {
      image = {
        repository = var.image_repository
        tag        = var.image_tag
      }
      publicUrl = var.public_url
      env       = "production"
      logFormat = "json"
      resultDir = "s3://${var.results_bucket}/results"

      oidc = {
        issuer   = var.oidc_issuer
        clientId = var.oidc_client_id
      }

      web = {
        replicaCount = var.replicas
      }
      autoscaling = {
        enabled     = true
        minReplicas = var.replicas
        maxReplicas = max(var.replicas * 3, 6)
      }

      worker = {
        mode = var.worker_mode
      }

      serviceAccount = {
        create     = true
        name       = "satchel"
        awsRoleArn = var.runtime_role_arn
        migration = {
          create     = true
          name       = "satchel-migrate"
          awsRoleArn = var.migrate_role_arn
        }
      }

      aws = {
        enabled = true
        region  = var.region
        alb = merge(
          { scheme = var.alb_scheme },
          var.certificate_arn == null ? {} : { certificateArn = var.certificate_arn },
          var.waf_acl_arn == null ? {} : { wafv2AclArn = var.waf_acl_arn },
        )
      }

      exposure = {
        type       = var.exposure
        host       = var.domain
        timeoutSec = 3600
      }

      networkPolicy = {
        enabled    = length(var.network_policy_cidrs) > 0
        allowCidrs = var.network_policy_cidrs
      }
    },
    # jsondecode/jsonencode sidesteps HCL's "inconsistent conditional result types" for objects.
    jsondecode(local.csi ? jsonencode(local.csi_values) : jsonencode(local.kubernetes_values)),
    var.chat_provider == null ? {} : {
      chat = merge(
        { provider = var.chat_provider },
        var.chat_model == null ? {} : { model = var.chat_model },
      )
    },
    var.session_retention_days == null ? {} : { sessionRetentionDays = var.session_retention_days },
    var.first_workspace == null ? {} : {
      firstWorkspace = {
        enabled    = true
        slug       = var.first_workspace.slug
        name       = var.first_workspace.name
        adminEmail = var.first_workspace.admin_email
      }
    },
    var.extra_values,
  )
}

output "values_yaml" {
  value = yamlencode(local.values)
}
