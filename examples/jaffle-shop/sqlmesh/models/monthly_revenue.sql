MODEL (
  name analytics.monthly_revenue,
  kind VIEW,
  owner 'finance@jaffle.example',
  description 'Revenue and order count by month, in AUD dollars, valid orders only (the weekly review deck reads it).',
  grain month,
  audits (unique_combination_of_columns(columns := (month)))
);

SELECT
  DATE_TRUNC('month', o.order_date) AS month,
  SUM(o.amount) / 100.0 AS revenue,
  COUNT(*) AS order_count
FROM analytics.orders AS o
JOIN analytics.customers AS c ON c.customer_id = o.customer_id
WHERE o.status NOT IN ('returned', 'return_pending')
GROUP BY 1
