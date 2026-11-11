# Pure function from inputs to the chart's values, kept free of resources so CI can render it
# and run `helm lint --strict` / `helm template` against deploy/helm/satchel (scripts/check-helm-values.sh).
# Keys mirror deploy/helm/satchel/values.yaml; the chart's values.schema.json is strict.
locals {
  values = merge(
    {
      image = {
        repository = var.image_repository
        tag        = var.image_tag
      }
      publicUrl      = var.public_url
      env            = "production"
      logFormat      = "json"
      resultDir      = "gs://${var.results_bucket}/results"
      existingSecret = var.existing_secret_name

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
        mode = "cronjob"
      }

      serviceAccount = {
        create            = true
        name              = var.service_account_name
        gcpServiceAccount = var.gcp_service_account_email
      }

      cloudSqlProxy = {
        enabled                = true
        instanceConnectionName = var.instance_connection_name
        privateIp              = true
      }

      bootstrap = {
        enabled        = var.bootstrap_enabled
        existingSecret = var.bootstrap_secret_name
      }

      gke = {
        enabled = true
      }

      exposure = {
        type       = "gateway"
        host       = var.domain
        timeoutSec = 3600
      }

      # The chart creates the Gateway (global external managed L7).
      gateway = merge(
        {
          create    = true
          className = "gke-l7-global-external-managed"
        },
        var.certificate_map == null ? {} : { certificateMap = var.certificate_map },
        var.static_ip_name == null ? {} : { staticIpName = var.static_ip_name },
      )
    },
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
