-- ============================================================================
-- Product Catalog Database Roles and Permissions
-- ============================================================================
-- This script creates PostgreSQL roles and grants permissions for the
-- product_catalog_db database.
--
-- Usage:
--   psql -U pixelforge -d product_catalog_db -f sql/roles.sql
--
-- Or run via management command:
--   python manage.py setup_db_roles
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. Create roles (if not exists)
-- ---------------------------------------------------------------------------

-- Owner role: full database administration (migrations, schema changes)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = '3654') THEN
        CREATE ROLE product_catalog_owner WITH LOGIN PASSWORD '3654';
    END IF;
END
$$;

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

-- Grant access to future tables in the schema
ALTER DEFAULT PRIVILEGES IN SCHEMA product_catalog
    GRANT SELECT ON TABLES TO product_catalog_readonly;

ALTER DEFAULT PRIVILEGES IN SCHEMA product_catalog
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO product_catalog_app;

-- ---------------------------------------------------------------------------
-- 4. Grant permissions on existing tables
--    (Run after migrations are applied)
-- ---------------------------------------------------------------------------

-- Application role permissions
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA product_catalog TO product_catalog_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA product_catalog TO product_catalog_app;

-- Read-only role permissions
GRANT SELECT ON ALL TABLES IN SCHEMA product_catalog TO product_catalog_readonly;

-- ---------------------------------------------------------------------------
-- 5. Restrict privileges
-- ---------------------------------------------------------------------------

-- Revoke dangerous permissions from app role
REVOKE CREATE ON SCHEMA product_catalog FROM product_catalog_app;
REVOKE CREATE ON DATABASE product_catalog_db FROM product_catalog_app;

-- Revoke all write permissions from readonly role
REVOKE INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA product_catalog FROM product_catalog_readonly;
REVOKE USAGE, SELECT ON ALL SEQUENCES IN SCHEMA product_catalog FROM product_catalog_readonly;

-- ---------------------------------------------------------------------------
-- 6. Verify setup
-- ---------------------------------------------------------------------------

-- Display role memberships
SELECT
    r.rolname AS role_name,
    r.rolsuper AS is_superuser,
    r.rolcreaterole AS can_create_roles,
    r.rolcreatedb AS can_create_db,
    r.rolcanlogin AS can_login
FROM pg_catalog.pg_roles r
WHERE r.rolname IN ('product_catalog_owner', 'product_catalog_app', 'product_catalog_readonly')
ORDER BY r.rolname;

-- Display table permissions
SELECT
    grantee,
    table_schema,
    table_name,
    privilege_type
FROM information_schema.role_table_grants
WHERE table_schema = 'product_catalog'
ORDER BY grantee, table_name, privilege_type;
