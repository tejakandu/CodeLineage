CREATE OR REPLACE TABLE {{ params.mart_dataset }}.fct_daily_revenue AS
WITH orders_enriched AS (
  SELECT
    o.order_date,
    c.region,
    o.net_amount_usd
  FROM {{ params.staging_dataset }}.int_orders_usd o
  JOIN {{ params.staging_dataset }}.stg_customers c
    ON o.customer_id = c.customer_id
)
SELECT
  order_date,
  region,
  SUM(net_amount_usd) AS net_revenue,
  COUNT(*)            AS order_count
FROM orders_enriched
GROUP BY order_date, region;
