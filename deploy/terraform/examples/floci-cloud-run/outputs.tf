output "public_url" {
  value = local.public_url
}

output "job_names" {
  value = module.cloud_run.job_names
}

output "instance_name" {
  value = module.foundation.instance_name
}

output "results_bucket" {
  value = module.foundation.results_bucket
}

output "service_name" {
  value = module.cloud_run.service_name
}
