# Cloud Run can only pull from Google registries (Artifact Registry, Container Registry) and,
# through a remote repository, from upstream registries. It cannot pull ghcr.io directly, so the
# default is an Artifact Registry remote repository that proxies ghcr.io (the satchel image is public,
# so no upstream credentials are needed).
resource "google_artifact_registry_repository" "ghcr" {
  count = var.create_ghcr_remote_repository ? 1 : 0

  project       = var.project_id
  location      = var.region
  repository_id = "${var.name}-ghcr"
  description   = "Remote repository proxying ghcr.io for satchel"
  format        = "DOCKER"
  mode          = "REMOTE_REPOSITORY"
  labels        = local.labels

  remote_repository_config {
    description = "ghcr.io"
    docker_repository {
      custom_repository {
        uri = "https://ghcr.io"
      }
    }
  }

  depends_on = [google_project_service.apis]
}

locals {
  # ghcr.io/getsatchel/satchel -> REGION-docker.pkg.dev/PROJECT/satchel-ghcr/getsatchel/satchel
  upstream_path = trimprefix(var.upstream_image_repository, "ghcr.io/")

  image_repository = var.create_ghcr_remote_repository ? (
    "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.ghcr[0].repository_id}/${local.upstream_path}"
    ) : (
    coalesce(var.image_repository_override, var.upstream_image_repository)
  )
}
