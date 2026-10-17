-- Unqualified columns across a join and SELECT * EXCEPT: both need INFORMATION_SCHEMA to resolve.
CREATE OR REPLACE TABLE `{{ params.project }}.{{ params.staging_dataset }}.stg_payments` AS
SELECT
  payment_id,
  order_id,
  method_name,
  amount_cents / 100 AS amount_usd
FROM `{{ params.project }}.{{ params.raw_dataset }}.payments`
JOIN `{{ params.project }}.{{ params.raw_dataset }}.payment_methods` USING (method_id);

CREATE OR REPLACE VIEW {{ params.staging_dataset }}.v_payments_masked AS
SELECT * EXCEPT (card_number) FROM {{ params.raw_dataset }}.payments;
