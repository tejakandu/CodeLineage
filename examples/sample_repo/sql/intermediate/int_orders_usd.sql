-- Convert local-currency order amounts to USD
INSERT INTO {{ params.staging_dataset }}.int_orders_usd (order_id, customer_id, order_date, net_amount_usd)
SELECT
  so.order_id,
  so.customer_id,
  so.order_date,
  ROUND(so.net_amount_local * fx.usd_rate, 2)
FROM {{ params.staging_dataset }}.stg_orders so
JOIN {{ params.fx_table }} fx
  ON fx.currency_code = so.currency_code
 AND fx.rate_date = so.order_date
WHERE so.status = 'COMPLETE';
