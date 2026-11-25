-- Runs once, when the local database volume is first created.
-- Extensions the schema relies on: vector search, UUID/crypto helpers, case-insensitive text.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;
