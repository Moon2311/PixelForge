from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from apps.models import Role, UserProfile


class Command(BaseCommand):
    help = "Seed roles and users"

    def handle(self, *args, **options):
        roles_data = ["admin", "buyer", "inventory_manager"]
        roles = {}
        for name in roles_data:
            role, created = Role.objects.get_or_create(name=name)
            roles[name] = role
            self.stdout.write(f"  {'Created' if created else 'Found'} role: {name}")

        users_data = [
            {
                "username": "admin",
                "email": "admin@pixelforge.com",
                "password": "admin123",
                "role": "admin",
                "is_staff": True,
            },
            {
                "username": "buyer",
                "email": "buyer@pixelforge.com",
                "password": "buyer123",
                "role": "buyer",
            },
            {
                "username": "inventory_manager",
                "email": "inventory@pixelforge.com",
                "password": "inventory123",
                "role": "inventory_manager",
            },
        ]

        for data in users_data:
            role_name = data.pop("role")
            is_staff = data.pop("is_staff", False)
            user, created = User.objects.get_or_create(
                username=data["username"],
                defaults={**data, "is_staff": is_staff},
            )
            if created:
                user.set_password(data["password"])
                user.save()
                self.stdout.write(f"  Created user: {user.username}")
            else:
                self.stdout.write(f"  Found user: {user.username}")

            UserProfile.objects.get_or_create(
                user=user, defaults={"role": roles[role_name]}
            )

        self.stdout.write(self.style.SUCCESS("Done seeding roles and users"))
