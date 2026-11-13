-- Rollback Migration: 001_initial_schema.rollback.sql
-- Description: Clean teardown of initial schema, triggers, and functions

-- 1. Drop Triggers
DROP TRIGGER IF EXISTS trg_ledger_prevent_truncate ON interaction_ledger;
DROP TRIGGER IF EXISTS trg_ledger_prevent_delete ON interaction_ledger;
DROP TRIGGER IF EXISTS trg_ledger_immutability ON interaction_ledger;

-- 2. Drop Functions
DROP FUNCTION IF EXISTS fn_prevent_ledger_truncate();
DROP FUNCTION IF EXISTS fn_prevent_ledger_delete();
DROP FUNCTION IF EXISTS fn_enforce_ledger_immutability();

-- 3. Drop Tables (in reverse dependency order)
DROP TABLE IF EXISTS interaction_ledger CASCADE;
DROP TABLE IF EXISTS lead_rejections CASCADE;
DROP TABLE IF EXISTS leads CASCADE;
DROP TABLE IF EXISTS dispositions CASCADE;
DROP TABLE IF EXISTS ingestion_file_checkpoints CASCADE;
DROP TABLE IF EXISTS ingestion_runs CASCADE;
