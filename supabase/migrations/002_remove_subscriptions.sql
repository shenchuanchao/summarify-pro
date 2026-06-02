-- 002_remove_subscriptions.sql
-- Remove PayPal subscription tables and related objects
-- After removing Premium payment module, these are no longer needed.

-- 1. Drop the trigger that syncs subscriptions -> users.plan
DROP TRIGGER IF EXISTS sync_plan_on_change ON subscriptions;

-- 2. Drop the trigger function
DROP FUNCTION IF EXISTS sync_plan_on_change();

-- 3. Drop the subscriptions table
DROP TABLE IF EXISTS subscriptions;

-- 4. Clean up users.plan column (set default to 'free' / remove premium values)
-- Option A: Update all users to 'free' plan (recommended)
UPDATE users SET plan = 'free' WHERE plan = 'premium';

-- Option B: If you prefer to keep the plan column but drop premium:
-- ALTER TABLE users DROP COLUMN IF EXISTS plan;
-- ALTER TABLE users ADD COLUMN IF NOT EXISTS plan TEXT DEFAULT 'free';