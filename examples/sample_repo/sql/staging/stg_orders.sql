-- Clean raw orders coming from the OLTP export
CREATE OR REPLACE TABLE `{{ params.project }}.{{ params.staging_dataset }}.stg_orders` AS
SELECT
  o.order_id,
  o.customer_id,
  CAST(o.order_ts AS DATE)            AS order_date,
  UPPER(TRIM(o.currency_code))        AS currency_code,
  o.gross_amount - COALESCE(o.discount_amount, 0) AS net_amount_local,
  o.status
FROM `{{ params.project }}.{{ params.raw_dataset }}.orders` AS o
WHERE o.status != 'TEST'
  AND DATE(o.order_ts) <= '{{ ds }}';
