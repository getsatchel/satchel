output "results_bucket" {
  value = module.foundation.results_bucket
}

output "audit_bucket" {
  value = module.foundation.audit_bucket
}

output "database_identifier" {
  value = module.foundation.database_identifier
}

output "runtime_role_arn" {
  value = module.eks.runtime_role_arn
}

output "migrate_role_arn" {
  value = module.eks.migrate_role_arn
}

output "waf_acl_arn" {
  value = module.eks.waf_acl_arn
}

output "certificate_arn" {
  value = module.eks.certificate_arn
}

output "secret_names" {
  value = module.foundation.secret_names
}
