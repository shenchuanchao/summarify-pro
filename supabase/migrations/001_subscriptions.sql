-- ============================================================
-- Summarify Pro — Generic Subscription Schema
-- Supports PayPal + Stripe (and future providers)
-- ============================================================

-- 1. Create generic subscriptions table
CREATE TABLE IF NOT EXISTS subscriptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider TEXT NOT NULL CHECK (provider IN ('paypal', 'stripe')),
    provider_subscription_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'active', 'cancelled', 'expired', 'past_due')),
    plan_tier TEXT NOT NULL DEFAULT 'premium',
    current_period_start TIMESTAMPTZ,
    current_period_end TIMESTAMPTZ,
    cancel_at_period_end BOOLEAN NOT NULL DEFAULT false,
    cancelled_at TIMESTAMPTZ,
    provider_metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 2. Indexes
CREATE INDEX IF NOT EXISTS idx_subscriptions_user_id ON subscriptions(user_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_provider ON subscriptions(provider, provider_subscription_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_status ON subscriptions(user_id, status);

-- 3. Function: sync users.plan from active subscriptions
CREATE OR REPLACE FUNCTION sync_user_plan_from_subscriptions()
RETURNS trigger AS $$
BEGIN
    -- After INSERT/UPDATE on subscriptions, update users.plan
    IF (TG_OP = 'INSERT' OR TG_OP = 'UPDATE') AND NEW.status = 'active' THEN
        UPDATE users SET plan = NEW.plan_tier WHERE id = NEW.user_id;
    END IF;

    -- If no more active subscriptions, downgrade to free
    IF (TG_OP = 'UPDATE' OR TG_OP = 'DELETE') AND
       (TG_OP = 'DELETE' OR OLD.status = 'active') AND
       (TG_OP = 'DELETE' OR NEW.status != 'active') THEN
        -- Check if user has other active subscriptions
        IF NOT EXISTS (
            SELECT 1 FROM subscriptions
            WHERE user_id = COALESCE(NEW.user_id, OLD.user_id)
              AND status = 'active'
              AND (TG_OP = 'DELETE' OR id != COALESCE(NEW.id, OLD.id))
        ) THEN
            UPDATE users SET plan = 'free'
            WHERE id = COALESCE(NEW.user_id, OLD.user_id);
        END IF;
    END IF;

    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

-- 4. Trigger
DROP TRIGGER IF EXISTS trg_sync_user_plan ON subscriptions;
CREATE TRIGGER trg_sync_user_plan
    AFTER INSERT OR UPDATE OR DELETE ON subscriptions
    FOR EACH ROW EXECUTE FUNCTION sync_user_plan_from_subscriptions();

-- 5. Migration: if paypal_subscription_id exists on users, move to subscriptions
DO $$
DECLARE
    u RECORD;
BEGIN
    -- Check if paypal_subscription_id column exists on users
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'users' AND column_name = 'paypal_subscription_id'
    ) THEN
        FOR u IN SELECT * FROM users WHERE paypal_subscription_id IS NOT NULL LOOP
            INSERT INTO subscriptions (
                user_id, provider, provider_subscription_id,
                status, plan_tier, created_at
            ) VALUES (
                u.id, 'paypal', u.paypal_subscription_id,
                'active', u.plan, NOW()
            )
            ON CONFLICT DO NOTHING;
        END LOOP;
    END IF;
END $$;