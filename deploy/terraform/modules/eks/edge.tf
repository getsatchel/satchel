# --- TLS certificate -----------------------------------------------------------

resource "aws_acm_certificate" "this" {
  count = var.certificate_arn == null ? 1 : 0

  domain_name       = var.domain
  validation_method = "DNS"
  tags              = local.tags

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "certificate_validation" {
  for_each = var.certificate_arn == null && var.route53_zone_id != null ? {
    for o in aws_acm_certificate.this[0].domain_validation_options : o.domain_name => o
  } : {}

  zone_id         = var.route53_zone_id
  name            = each.value.resource_record_name
  type            = each.value.resource_record_type
  records         = [each.value.resource_record_value]
  ttl             = 300
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "this" {
  count = var.certificate_arn == null && var.route53_zone_id != null ? 1 : 0

  certificate_arn         = aws_acm_certificate.this[0].arn
  validation_record_fqdns = [for r in aws_route53_record.certificate_validation : r.fqdn]
}

locals {
  certificate_arn = coalesce(
    var.certificate_arn,
    try(aws_acm_certificate_validation.this[0].certificate_arn, null),
    try(aws_acm_certificate.this[0].arn, null),
  )
}

# --- WAF -----------------------------------------------------------------------

locals {
  waf_managed_rules = {
    AWSManagedRulesAmazonIpReputationList = 10
    AWSManagedRulesCommonRuleSet          = 20
    AWSManagedRulesKnownBadInputsRuleSet  = 30
  }
}

resource "aws_wafv2_web_acl" "this" {
  count = var.enable_waf && var.waf_acl_arn == null ? 1 : 0

  name  = var.name
  scope = "REGIONAL"
  tags  = local.tags

  default_action {
    allow {}
  }

  dynamic "rule" {
    for_each = local.waf_managed_rules
    content {
      name     = rule.key
      priority = rule.value

      override_action {
        none {}
      }

      statement {
        managed_rule_group_statement {
          vendor_name = "AWS"
          name        = rule.key

          # MCP and the curator API carry large JSON bodies (SQL, context items); the common
          # rule set's 8 KB body limit would block them, so that one rule only counts.
          dynamic "rule_action_override" {
            for_each = rule.key == "AWSManagedRulesCommonRuleSet" ? ["SizeRestrictions_BODY"] : []
            content {
              name = rule_action_override.value
              action_to_use {
                count {}
              }
            }
          }
        }
      }

      visibility_config {
        cloudwatch_metrics_enabled = true
        metric_name                = "${var.name}-${rule.key}"
        sampled_requests_enabled   = true
      }
    }
  }

  rule {
    name     = "rate-limit-per-ip"
    priority = 40

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit              = var.waf_rate_limit
        aggregate_key_type = "IP"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.name}-rate-limit"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = var.name
    sampled_requests_enabled   = true
  }
}

locals {
  waf_acl_arn = var.waf_acl_arn != null ? var.waf_acl_arn : try(aws_wafv2_web_acl.this[0].arn, null)
}
