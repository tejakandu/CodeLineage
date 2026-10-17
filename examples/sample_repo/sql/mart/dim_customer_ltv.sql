-- Customer lifetime value. BigQuery scripting: variables, a temp table, a procedure with UPDATE.
DECLARE as_of DATE DEFAULT DATE '{{ ds }}';

CREATE TEMP TABLE tmp_customer_orders AS
SELECT customer_id, order_date, net_amount_usd
FROM {{ params.staging_dataset }}.int_orders_usd;

CREATE OR REPLACE TABLE {{ params.mart_dataset }}.dim_customer_ltv AS
WITH agg AS (
  SELECT
    customer_id,
    SUM(net_amount_usd) AS lifetime_value,
    MIN(order_date)     AS first_order_date,
    MAX(order_date)     AS last_order_date
  FROM tmp_customer_orders
  GROUP BY customer_id
)
SELECT
  c.customer_id,
  c.customer_name,
  c.region,
  a.lifetime_value,
  a.first_order_date,
  DATE_DIFF(as_of, a.last_order_date, DAY) AS days_since_last_order,
  CASE
    WHEN a.lifetime_value >= 1000 THEN 'GOLD'
    WHEN a.lifetime_value >= 250  THEN 'SILVER'
    ELSE 'BRONZE'
  END AS tier
FROM agg a
JOIN {{ params.staging_dataset }}.stg_customers c ON a.customer_id = c.customer_id;

CREATE OR REPLACE PROCEDURE {{ params.mart_dataset }}.apply_vip_overrides()
BEGIN
  UPDATE {{ params.mart_dataset }}.dim_customer_ltv d
  SET tier = f.vip_tier
  FROM {{ params.raw_dataset }}.customer_flags f
  WHERE d.customer_id = f.customer_id AND f.vip_tier IS NOT NULL;
END;
