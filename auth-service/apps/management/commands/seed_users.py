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
                "email": "talhaabdulsattarnrg@gmail.com",
                "password": "Admin@123",
                "role": "admin",
                "is_staff": True,
                "is_superuser": True,
            },
            {
                "username": "talha",
                "email": "talhaabdulsattar018@gmail.com",
                "password": "Talha@pak",
                "role": "inventory_manager",
                "is_staff": True,
            },
        ]

        for data in users_data:
            role_name = data.pop("role")
            is_staff = data.pop("is_staff", False)
            is_superuser = data.pop("is_superuser", False)
            user, created = User.objects.get_or_create(
                username=data["username"],
                defaults={**data, "is_staff": is_staff, "is_superuser": is_superuser},
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
