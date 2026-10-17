from datetime import datetime

import yaml
from airflow import DAG
from airflow.providers.google.cloud.operators.bigquery import BigQueryInsertJobOperator
from airflow.providers.common.sql.operators.sql import SQLExecuteQueryOperator

CONFIG = yaml.safe_load(open("/opt/airflow/config/env_prod.yaml"))

with DAG(
    dag_id="daily_sales",
    schedule="0 3 * * *",
    start_date=datetime(2024, 1, 1),
    template_searchpath=["/opt/airflow/sql"],
    params=CONFIG,
    catchup=False,
) as dag:
    stg_orders = BigQueryInsertJobOperator(
        task_id="stg_orders",
        configuration={"query": {"query": "{% include 'staging/stg_orders.sql' %}", "useLegacySql": False}},
    )
    stg_customers = BigQueryInsertJobOperator(
        task_id="stg_customers",
        configuration={"query": {"query": "{% include 'staging/stg_customers.sql' %}", "useLegacySql": False}},
    )
    int_orders_usd = SQLExecuteQueryOperator(
        task_id="int_orders_usd",
        sql="intermediate/int_orders_usd.sql",
    )
    fct_daily_revenue = SQLExecuteQueryOperator(
        task_id="fct_daily_revenue",
        sql="mart/fct_daily_revenue.sql",
    )

    stg_orders >> int_orders_usd
    [int_orders_usd, stg_customers] >> fct_daily_revenue
