"""Management command to test database connectivity.

Usage:
    python manage.py test_db_connection
"""

from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Test PostgreSQL database connectivity"

    def handle(self, *args, **options):
        self.stdout.write("Testing database connection...")

        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT version()")
                db_version = cursor.fetchone()[0]
                self.stdout.write(self.style.SUCCESS(f"Connected to: {db_version}"))

                cursor.execute("SELECT current_database(), current_user")
                db_name, db_user = cursor.fetchone()
                self.stdout.write(self.style.SUCCESS(f"Database: {db_name}"))
                self.stdout.write(self.style.SUCCESS(f"User: {db_user}"))

                cursor.execute("""
                    SELECT table_name 
                    FROM information_schema.tables 
                    WHERE table_schema = 'public'
                    ORDER BY table_name
                """)
                tables = [row[0] for row in cursor.fetchall()]
                if tables:
                    self.stdout.write(f"Tables: {', '.join(tables)}")
                else:
                    self.stdout.write("No tables found (run migrations first)")

        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Connection failed: {e}"))
            raise
