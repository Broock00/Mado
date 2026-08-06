-- Extensions required by the Mado data platform (spec 54.09 / 80.04).
--   postgis   -> geography columns for venue coordinates and proximity search
--   vector    -> embedding storage for semantic retrieval and AI memory
--   pg_trgm   -> trigram matching for typo-tolerant fallback when Meilisearch is down
--   uuid-ossp -> gen_random_uuid is built in on PG16, kept for older tooling parity
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- One schema per owning domain. Spec 80.04 forbids cross-domain table access;
-- separate schemas make that boundary visible and enforceable via grants later.
CREATE SCHEMA IF NOT EXISTS identity;
CREATE SCHEMA IF NOT EXISTS publisher;
CREATE SCHEMA IF NOT EXISTS catalog;
CREATE SCHEMA IF NOT EXISTS explorer;
CREATE SCHEMA IF NOT EXISTS ai;
