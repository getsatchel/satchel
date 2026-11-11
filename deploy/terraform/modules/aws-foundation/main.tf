data "aws_caller_identity" "this" {}
data "aws_partition" "this" {}
data "aws_region" "this" {}

data "aws_availability_zones" "available" {
  count = var.create_vpc && var.availability_zones == null ? 1 : 0
  state = "available"
}

locals {
  tags         = merge({ app = "satchel", managed-by = "terraform" }, var.tags)
  account_id   = data.aws_caller_identity.this.account_id
  partition    = data.aws_partition.this.partition
  region       = data.aws_region.this.region
  cluster_name = coalesce(var.cluster_name, var.name)

  bucket_prefix = coalesce(var.bucket_name_prefix, "${local.account_id}-${var.name}")
}
