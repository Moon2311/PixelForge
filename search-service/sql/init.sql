-- ============================================================================
-- PostgreSQL Initialization Script
-- ============================================================================
-- This script runs when the PostgreSQL container starts for the first time.
-- It creates the application roles and grants permissions.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. Create application roles
-- ---------------------------------------------------------------------------

-- Application role: Django runtime (SELECT, INSERT, UPDATE, DELETE)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'product_catalog_app') THEN
        CREATE ROLE product_catalog_app WITH LOGIN PASSWORD '3654';
    END IF;
END
$$;

-- Read-only role: reporting/analytics (SELECT only)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'product_catalog_readonly') THEN
        CREATE ROLE product_catalog_readonly WITH LOGIN PASSWORD '3654';
    END IF;
END
$$;

-- ---------------------------------------------------------------------------
-- 2. Create dedicated schema
-- ---------------------------------------------------------------------------

CREATE SCHEMA IF NOT EXISTS product_catalog AUTHORIZATION product_catalog_owner;

-- ---------------------------------------------------------------------------
-- 3. Grant schema usage
-- ---------------------------------------------------------------------------

GRANT USAGE ON SCHEMA product_catalog TO product_catalog_app;
GRANT USAGE ON SCHEMA product_catalog TO product_catalog_readonly;

-- Also grant usage on public schema (Django default)
GRANT USAGE ON SCHEMA public TO product_catalog_app;
GRANT USAGE ON SCHEMA public TO product_catalog_readonly;

-- Grant CREATE on public schema for migrations
GRANT CREATE ON SCHEMA public TO product_catalog_owner;

-- ---------------------------------------------------------------------------
-- 4. Set default privileges for future tables
-- ---------------------------------------------------------------------------

-- Read-only role gets SELECT on future tables
ALTER DEFAULT PRIVILEGES IN SCHEMA product_catalog
    GRANT SELECT ON TABLES TO product_catalog_readonly;

-- App role gets full CRUD on future tables
ALTER DEFAULT PRIVILEGES IN SCHEMA product_catalog
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO product_catalog_app;

-- App role gets sequence usage for future sequences
ALTER DEFAULT PRIVILEGES IN SCHEMA product_catalog
    GRANT USAGE, SELECT ON SEQUENCES TO product_catalog_app;

-- Grant app role full access to public schema (for Django)
GRANT ALL PRIVILEGES ON SCHEMA public TO product_catalog_app;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO product_catalog_app;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO product_catalog_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO product_catalog_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO product_catalog_app;

-- ---------------------------------------------------------------------------
-- 5. Log completion
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    RAISE NOTICE 'Product Catalog database roles initialized successfully';
    RAISE NOTICE 'Roles: product_catalog_owner, product_catalog_app, product_catalog_readonly';
    RAISE NOTICE 'Schema: product_catalog';
END
$$;
