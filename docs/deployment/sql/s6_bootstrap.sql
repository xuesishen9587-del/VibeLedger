-- Fresh identity/category bootstrap only. No account, token, schedule or financial data.
\set ON_ERROR_STOP on
SELECT :'authorization' = 'BOOTSTRAP_NEW_VIBELEDGER_PROD_V1'
   AND current_database() = :'expected_database'
   AND session_user = :'expected_operator' AS permitted \gset
\if :permitted
\else
  \echo 'STOP: explicit bootstrap authorization/database/operator mismatch'
  DO $$ BEGIN RAISE EXCEPTION 'S6 operator precondition failed; no changes applied'; END $$;
\endif
-- Private operator inputs: do not record this terminal or commit prompt answers.
\prompt 'Household display name: ' household_name
\prompt 'Reporting currency (CNY/SGD/USD/EUR/JPY): ' reporting_currency
\prompt 'Household started_on (YYYY-MM-DD; agreed capture start): ' started_on
\prompt 'Owner Supabase Auth UUID: ' owner_subject
\prompt 'Owner verified email: ' owner_email
\prompt 'Owner display name: ' owner_name
\prompt 'Member Supabase Auth UUID (different person): ' member_subject
\prompt 'Member verified email: ' member_email
\prompt 'Member display name: ' member_name
BEGIN;
SELECT pg_advisory_xact_lock(hashtext('vibeledger:s6:prod:v1'));
SELECT :'owner_subject'::uuid <> :'member_subject'::uuid
  AND :'reporting_currency' IN ('CNY','SGD','USD','EUR','JPY')
  AND :'started_on'::date <= (now() AT TIME ZONE 'Asia/Singapore')::date
  AND length(trim(:'household_name')) > 0
  AND EXISTS (SELECT 1 FROM auth.users WHERE id=:'owner_subject'::uuid AND lower(email)=lower(:'owner_email') AND email_confirmed_at IS NOT NULL)
  AND EXISTS (SELECT 1 FROM auth.users WHERE id=:'member_subject'::uuid AND lower(email)=lower(:'member_email') AND email_confirmed_at IS NOT NULL)
  AS identities_valid \gset
\if :identities_valid
\else
  ROLLBACK;
  \echo 'STOP: verified identities, currency or start date do not match'
  DO $$ BEGIN RAISE EXCEPTION 'S6 operator precondition failed; no changes applied'; END $$;
\endif
SET LOCAL ROLE vibeledger_prod_owner;
SET LOCAL search_path TO vibeledger_prod_v1, pg_catalog;
DO $$
DECLARE t text; occupied boolean;
BEGIN
  FOR t IN SELECT table_name FROM information_schema.tables WHERE table_schema='vibeledger_prod_v1' AND table_name <> 'schema_migrations' LOOP
    EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I.%I)', 'vibeledger_prod_v1', t) INTO occupied;
    IF occupied THEN RAISE EXCEPTION 'Fresh bootstrap requires all application tables empty'; END IF;
  END LOOP;
  IF (SELECT count(*) FROM schema_migrations WHERE migration_name='0001_simplified.sql' AND checksum_sha256='bf8cc2ea6c46dd9ce66389784e499c1bbd7ff756ef532d1d9b23aa83d186651c') <> 1 THEN
    RAISE EXCEPTION 'Wrong migration baseline';
  END IF;
END $$;
INSERT INTO households(name,reporting_currency,started_on,timezone)
VALUES (trim(:'household_name'),:'reporting_currency',:'started_on'::date,'Asia/Singapore') RETURNING id AS household_id \gset
INSERT INTO users(auth_subject,email,display_name) VALUES (:'owner_subject',:'owner_email',:'owner_name') RETURNING id AS owner_id \gset
INSERT INTO users(auth_subject,email,display_name) VALUES (:'member_subject',:'member_email',:'member_name') RETURNING id AS member_id \gset
INSERT INTO household_members(household_id,user_id,role) VALUES
 (:'household_id'::uuid,:'owner_id'::uuid,'owner'), (:'household_id'::uuid,:'member_id'::uuid,'member');
INSERT INTO categories(household_id,name,description,category_type,is_fallback)
SELECT :'household_id'::uuid,v.name,v.description,v.category_type,v.is_fallback FROM (VALUES
  ('Grocery', 'Groceries, household consumables, ingredients', 'expense', false),
  ('Dine', 'Restaurants, takeaway, coffee, drinks and ready-to-eat snacks', 'expense', false),
  ('Child', 'All explicitly child-related purchases, including health, clothing and education; precedes those adult categories', 'expense', false),
  ('Home & Utilities', 'Rent, utilities, maintenance, appliances; excludes loan principal, includes identifiable mortgage interest', 'expense', false),
  ('Digital & Gadgets', 'Phones, computers, accessories and electronics', 'expense', false),
  ('Clothing', 'Adult clothing, shoes and accessories', 'expense', false),
  ('Beauty', 'Adult skincare, cosmetics, haircuts and personal care', 'expense', false),
  ('Transportation', 'Transit, taxi, fuel, parking, maintenance and tolls', 'expense', false),
  ('Health', 'Adult medicine, care, checkups and medical insurance', 'expense', false),
  ('Education', 'Adult books, training, software and AI subscriptions', 'expense', false),
  ('Gift & Socials', 'Gifts, social occasions and cash gifts to parents', 'expense', false),
  ('Parents', 'Specific goods/services for parents, excluding cash gifts', 'expense', false),
  ('Fun & Games', 'Routine entertainment, games, cinema and recreation', 'expense', false),
  ('Trips & Occasions', 'Holidays, anniversaries and distinct major occasions', 'expense', false),
  ('Other', 'Clear expenses without a sufficiently reliable category', 'expense', true),
  ('Salary', 'Regular wages, salary, and bonuses', 'income', false),
  ('Interest', 'Interest, dividends, and yields', 'income', false),
  ('Other income', 'Other miscellaneous household income', 'income', true)
) AS v(name,description,category_type,is_fallback);
COMMIT;
\echo 'Bootstrap committed: one household, two memberships, 18 categories; no financial data'
