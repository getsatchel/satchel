output "cluster_name" {
  value = module.gke.cluster_name
}

output "cluster_endpoint" {
  value = module.gke.cluster_endpoint
}

output "namespace" {
  value = module.gke.namespace
}

output "release_name" {
  value = module.gke.release_name
}

output "instance_name" {
  value = module.foundation.instance_name
}

output "results_bucket" {
  value = module.foundation.results_bucket
}
