"""
Database setup for cart-db.
Run as: psql -U postgres -f init.sql

Creates roles and database following the same pattern as search-service.
"""

-- Roles
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'cart_owner') THEN
        CREATE ROLE cart_owner WITH LOGIN PASSWORD 'owner_secret_change_me';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'cart_app') THEN
        CREATE ROLE cart_app WITH LOGIN PASSWORD 'cart_secret_change_me';
    END IF;
END
$$;

-- Database
SELECT 'CREATE DATABASE cart_db OWNER cart_owner'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'cart_db')\gexec

-- Grant privileges
GRANT ALL PRIVILEGES ON DATABASE cart_db TO cart_owner;

\connect cart_db

ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO cart_owner;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO cart_owner;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO cart_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO cart_app;

GRANT ALL ON SCHEMA public TO cart_owner;
GRANT ALL ON SCHEMA public TO cart_app;
