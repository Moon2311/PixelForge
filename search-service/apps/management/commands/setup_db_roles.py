"""Management command to set up PostgreSQL database roles and permissions.

Usage:
    python manage.py setup_db_roles
    python manage.py setup_db_roles --drop
    python manage.py setup_db_roles --verify
"""

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Set up PostgreSQL database roles and permissions for product_catalog_db"

    def add_arguments(self, parser):
        parser.add_argument(
            "--drop",
            action="store_true",
            help="Drop all product catalog roles (careful!)",
        )
        parser.add_argument(
            "--verify",
            action="store_true",
            help="Verify current role setup",
        )
        parser.add_argument(
            "--owner-password",
            type=str,
            default="owner_secret_change_me",
            help="Password for product_catalog_owner role",
        )
        parser.add_argument(
            "--app-password",
            type=str,
            default="app_secret_change_me",
            help="Password for product_catalog_app role",
        )
        parser.add_argument(
            "--readonly-password",
            type=str,
            default="readonly_secret_change_me",
            help="Password for product_catalog_readonly role",
        )

    def handle(self, *args, **options):
        if options["verify"]:
            self._verify_roles()
            return

        if options["drop"]:
            self._drop_roles()
            return

        self._create_roles(
            owner_password=options["owner_password"],
            app_password=options["app_password"],
            readonly_password=options["readonly_password"],
        )

    def _create_roles(self, owner_password, app_password, readonly_password):
        """Create PostgreSQL roles and grant permissions."""
        self.stdout.write("Setting up PostgreSQL roles...")

        with connection.cursor() as cursor:
            # 1. Create roles
            self.stdout.write("  Creating roles...")

            cursor.execute(f"""
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'product_catalog_owner') THEN
                        CREATE ROLE product_catalog_owner WITH LOGIN PASSWORD '{owner_password}';
                    ELSE
                        ALTER ROLE product_catalog_owner WITH PASSWORD '{owner_password}';
                    END IF;
                END
            $$;""")

            cursor.execute(f"""
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'product_catalog_app') THEN
                        CREATE ROLE product_catalog_app WITH LOGIN PASSWORD '{app_password}';
                    ELSE
                        ALTER ROLE product_catalog_app WITH PASSWORD '{app_password}';
                    END IF;
                END
            $$;""")

            cursor.execute(f"""
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'product_catalog_readonly') THEN
                        CREATE ROLE product_catalog_readonly WITH LOGIN PASSWORD '{readonly_password}';
                    ELSE
                        ALTER ROLE product_catalog_readonly WITH PASSWORD '{readonly_password}';
                    END IF;
                END
            $$;""")

            # 2. Create schema
            self.stdout.write("  Creating schema...")
            cursor.execute("CREATE SCHEMA IF NOT EXISTS product_catalog AUTHORIZATION product_catalog_owner")

            # 3. Grant schema usage
            self.stdout.write("  Granting schema usage...")
            cursor.execute("GRANT USAGE ON SCHEMA product_catalog TO product_catalog_app")
            cursor.execute("GRANT USAGE ON SCHEMA product_catalog TO product_catalog_readonly")

            # 4. Grant default privileges for future tables
            self.stdout.write("  Setting default privileges...")
            cursor.execute("""
                ALTER DEFAULT PRIVILEGES IN SCHEMA product_catalog
                GRANT SELECT ON TABLES TO product_catalog_readonly
            """)
            cursor.execute("""
                ALTER DEFAULT PRIVILEGES IN SCHEMA product_catalog
                GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO product_catalog_app
            """)
            cursor.execute("""
                ALTER DEFAULT PRIVILEGES IN SCHEMA product_catalog
                GRANT USAGE, SELECT ON SEQUENCES TO product_catalog_app
            """)

            # 5. Grant permissions on existing tables
            self.stdout.write("  Granting table permissions...")
            cursor.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA product_catalog TO product_catalog_app")
            cursor.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA product_catalog TO product_catalog_app")
            cursor.execute("GRANT SELECT ON ALL TABLES IN SCHEMA product_catalog TO product_catalog_readonly")

            # 6. Restrict privileges
            self.stdout.write("  Restricting privileges...")
            cursor.execute("REVOKE CREATE ON SCHEMA product_catalog FROM product_catalog_app")
            cursor.execute(f"REVOKE CREATE ON DATABASE {settings.DATABASES['default']['NAME']} FROM product_catalog_app")

        self.stdout.write(self.style.SUCCESS("PostgreSQL roles setup complete!"))
        self._verify_roles()

    def _drop_roles(self):
        """Drop all product catalog roles."""
        self.stdout.write(self.style.WARNING("Dropping PostgreSQL roles..."))

        with connection.cursor() as cursor:
            # Revoke all permissions first
            cursor.execute("REVOKE ALL ON ALL TABLES IN SCHEMA product_catalog FROM product_catalog_app")
            cursor.execute("REVOKE ALL ON ALL TABLES IN SCHEMA product_catalog FROM product_catalog_readonly")
            cursor.execute("REVOKE ALL ON SCHEMA product_catalog FROM product_catalog_app")
            cursor.execute("REVOKE ALL ON SCHEMA product_catalog FROM product_catalog_readonly")

            # Drop roles
            cursor.execute("DROP ROLE IF EXISTS product_catalog_app")
            cursor.execute("DROP ROLE IF EXISTS product_catalog_readonly")
            cursor.execute("DROP ROLE IF EXISTS product_catalog_owner")

        self.stdout.write(self.style.SUCCESS("PostgreSQL roles dropped!"))

    def _verify_roles(self):
        """Verify current role setup."""
        self.stdout.write("\nVerifying PostgreSQL roles...\n")

        with connection.cursor() as cursor:
            # Check roles
            cursor.execute("""
                SELECT
                    r.rolname,
                    r.rolsuper,
                    r.rolcreaterole,
                    r.rolcreatedb,
                    r.rolcanlogin
                FROM pg_catalog.pg_roles r
                WHERE r.rolname IN ('product_catalog_owner', 'product_catalog_app', 'product_catalog_readonly')
                ORDER BY r.rolname
            """)
            roles = cursor.fetchall()

            if roles:
                self.stdout.write("Roles:")
                self.stdout.write(f"  {'Role Name':<30} {'Superuser':<12} {'Create Role':<14} {'Create DB':<12} {'Can Login':<12}")
                self.stdout.write("  " + "-" * 80)
                for role in roles:
                    self.stdout.write(
                        f"  {role[0]:<30} {str(role[1]):<12} {str(role[2]):<14} {str(role[3]):<12} {str(role[4]):<12}"
                    )
            else:
                self.stdout.write(self.style.WARNING("No product catalog roles found!"))

            # Check schema
            cursor.execute("""
                SELECT schema_name
                FROM information_schema.schemata
                WHERE schema_name = 'product_catalog'
            """)
            schema = cursor.fetchone()
            if schema:
                self.stdout.write(f"\nSchema: {schema[0]} exists")
            else:
                self.stdout.write(self.style.WARNING("\nSchema 'product_catalog' not found!"))

            # Check table permissions
            cursor.execute("""
                SELECT
                    grantee,
                    table_name,
                    string_agg(DISTINCT privilege_type, ', ' ORDER BY privilege_type) as privileges
                FROM information_schema.role_table_grants
                WHERE table_schema = 'product_catalog'
                GROUP BY grantee, table_name
                ORDER BY grantee, table_name
            """)
            permissions = cursor.fetchall()

            if permissions:
                self.stdout.write("\nTable Permissions:")
                self.stdout.write(f"  {'Grantee':<30} {'Table':<30} {'Privileges'}")
                self.stdout.write("  " + "-" * 80)
                for perm in permissions:
                    self.stdout.write(f"  {perm[0]:<30} {perm[1]:<30} {perm[2]}")
            else:
                self.stdout.write("\nNo table permissions found (run migrations first)")

        self.stdout.write("")
