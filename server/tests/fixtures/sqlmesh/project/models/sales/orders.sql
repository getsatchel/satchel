/* A trimmed-down real-world model: 3-part name, an incremental kind, comments and a ')' in a
   string must not confuse the reader. */
MODEL (
  name warehouse.sales.orders,   -- the physical name, three parts
  kind INCREMENTAL_BY_TIME_RANGE (
    time_column order_ts,
    batch_size 10
  ),
  cron '@daily',
  start '2024-01-01',
  owner 'sales-eng@example.com',
  tags (sales, 'pii (some)'),
  description 'Order lines (one per item). Joined to dim_customer; a ")" and -- are fine here.',
  grains ((order_id, line_no), (order_id)),
  audits (
    unique_combination_of_columns(columns := (order_id, line_no)),
    not_null_non_blocking(columns := order_id),
    accepted_values(column := channel, is_in := ('web', 'store', 'phone')),
    accepted_values(column := region, is_in := ('x', 'y'), where := 'region IS NOT NULL'),
    number_of_rows(threshold := 10),
    my_custom_audit
  ),
  column_descriptions (
    order_id = 'Order number.',
    line_no = 'Line within the order.',
    channel = 'Where it was bought.'
  ),
  depends_on [warehouse.sales.dim_customer]
);

WITH recent AS (
  SELECT o.order_id, o.line_no, o.channel, o.order_ts, c.name AS customer_name
  FROM raw.order_lines AS o
  JOIN warehouse.sales.dim_customer AS c ON c.id = o.customer_id
)
SELECT order_id, line_no, channel, order_ts, customer_name FROM recent
