CREATE OR REPLACE VIEW {{ params.mart_dataset }}.rpt_region_kpis AS
SELECT
  region,
  SUM(net_revenue) / NULLIF(SUM(order_count), 0) AS avg_order_value,
  SUM(net_revenue)                               AS total_revenue
FROM {{ params.mart_dataset }}.fct_daily_revenue
GROUP BY region;
