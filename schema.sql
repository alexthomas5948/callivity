-- =============================================================================
-- Callivity Voice AI SaaS - Database Schema for Supabase PostgreSQL
-- =============================================================================
-- Table: call_records
-- Purpose: Logs billable call minutes, caller metadata, and recording links
--          for regional BPO overflow operations.
-- Run this in the Supabase Dashboard -> SQL Editor
-- =============================================================================

-- 1. Ensure the pgcrypto extension is enabled for gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- 2. Create the call_records table
CREATE TABLE IF NOT EXISTS public.call_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    room_name TEXT NOT NULL UNIQUE,
    caller_phone TEXT NOT NULL,
    start_time TIMESTAMPTZ NOT NULL,
    end_time TIMESTAMPTZ NOT NULL,
    duration_seconds INTEGER NOT NULL DEFAULT 0,
    -- Telecommunications BPO billing standard: ceiling round to the next whole minute
    billable_minutes INTEGER GENERATED ALWAYS AS (
        CEIL(duration_seconds::NUMERIC / 60.0)::INTEGER
    ) STORED,
    recording_url TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 3. Create high-performance indexes for ledger queries and phone lookup
CREATE INDEX IF NOT EXISTS idx_call_records_room_name ON public.call_records (room_name);
CREATE INDEX IF NOT EXISTS idx_call_records_caller_phone ON public.call_records (caller_phone);
CREATE INDEX IF NOT EXISTS idx_call_records_start_time ON public.call_records (start_time DESC);
CREATE INDEX IF NOT EXISTS idx_call_records_duration ON public.call_records (duration_seconds);

-- 4. Enable Row Level Security (RLS)
ALTER TABLE public.call_records ENABLE ROW LEVEL SECURITY;

-- 5. Row Level Security Policies
-- Allow backend service role (FastAPI webhook receiver) full read/write access
CREATE POLICY "Service role full access to call_records"
    ON public.call_records
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

-- Allow authenticated dashboard users (BPO managers / clients) read access to view metrics
CREATE POLICY "Authenticated users can view call records"
    ON public.call_records
    FOR SELECT
    TO authenticated
    USING (true);

-- 6. Trigger to automatically keep updated_at current
CREATE OR REPLACE FUNCTION public.update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_call_records_updated_at ON public.call_records;
CREATE TRIGGER trigger_call_records_updated_at
    BEFORE UPDATE ON public.call_records
    FOR EACH ROW
    EXECUTE FUNCTION public.update_updated_at_column();
