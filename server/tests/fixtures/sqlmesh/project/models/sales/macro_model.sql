MODEL (
  name sales.daily,
  kind VIEW,
  owner 'x@example.com',
  description 'Uses macros, so the query is not parsed.'
);

@DEF(min_ts, '2024-01-01');
SELECT order_id, @AGG(amount) AS total FROM warehouse.sales.orders WHERE order_ts > @min_ts
