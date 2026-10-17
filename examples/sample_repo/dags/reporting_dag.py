from datetime import datetime

from airflow import DAG
from airflow.providers.common.sql.operators.sql import SQLExecuteQueryOperator

with DAG("reporting", schedule="0 6 * * *", start_date=datetime(2024, 1, 1), catchup=False,
         params={"mart_dataset": "mart"}) as dag:
    region_kpis = SQLExecuteQueryOperator(task_id="rpt_region_kpis", sql="mart/rpt_region_kpis.sql")

    # inline SQL is picked up too
    customer_snapshot = SQLExecuteQueryOperator(
        task_id="customer_snapshot",
        sql="""
            INSERT INTO mart.customer_snapshot (customer_id, region, snapshot_date)
            SELECT customer_id, region, CURRENT_DATE() FROM staging.stg_customers
        """,
    )
