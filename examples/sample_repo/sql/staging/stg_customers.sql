CREATE OR REPLACE TABLE `{{ params.project }}.{{ params.staging_dataset }}.stg_customers` AS
WITH latest AS (
  SELECT
    c.*,
    ROW_NUMBER() OVER (PARTITION BY c.customer_id ORDER BY c.updated_at DESC) AS rn
  FROM `{{ params.project }}.{{ params.raw_dataset }}.customers` c
)
SELECT
  l.customer_id,
  l.customer_name,
  COALESCE(r.region_name, 'UNKNOWN') AS region
FROM latest l
LEFT JOIN `{{ params.project }}.ref.country_region_map` r
  ON l.country_code = r.country_code
WHERE l.rn = 1;
