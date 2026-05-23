-- ══════════════════════════════════════════════════════════════
-- Summarify Pro — Supabase Schema
-- Run these SQL files in Supabase Dashboard > SQL Editor
-- ══════════════════════════════════════════════════════════════

-- ── Users Table ────────────────────────────────────────────────
-- Stores registered user accounts with auth and usage tracking

CREATE TABLE IF NOT EXISTS users (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    email VARCHAR(255) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    plan VARCHAR(20) DEFAULT 'free',          -- 'free' or 'premium'
    daily_usage_count INTEGER DEFAULT 0,
    last_usage_date DATE,                     -- Tracks daily reset
    stripe_customer_id VARCHAR(255),
    stripe_session_id VARCHAR(255),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_stripe ON users(stripe_customer_id);

-- ── Feedback Table ─────────────────────────────────────────────
-- Stores user-submitted feedback (anonymous or logged-in)

CREATE TABLE IF NOT EXISTS feedback (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_feedback_user ON feedback(user_id);

-- ── Anonymous Usage Table ──────────────────────────────────────
-- Tracks free (non-registered) daily usage by a browser-generated anon_id

CREATE TABLE IF NOT EXISTS anonymous_usage (
    id BIGSERIAL PRIMARY KEY,
    anon_id VARCHAR(64) NOT NULL,              -- Frontend-generated UUID, stored in localStorage
    ip_address VARCHAR(45),                    -- IP address (for abuse detection)
    usage_date DATE NOT NULL,
    usage_count INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT unique_anon_date UNIQUE (anon_id, usage_date)
);

CREATE INDEX IF NOT EXISTS idx_anon_usage_lookup ON anonymous_usage(anon_id, usage_date);

-- ── Row Level Security (optional) ──────────────────────────────
-- Service role key bypasses RLS — these policies allow it full access.

-- ALTER TABLE users ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY "Service role full access" ON users FOR ALL USING (true);

-- ALTER TABLE feedback ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY "Service role full access" ON feedback FOR ALL USING (true);

-- ALTER TABLE anonymous_usage ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY "Service role full access" ON anonymous_usage FOR ALL USING (true);