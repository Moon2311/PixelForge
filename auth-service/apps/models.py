from django.db import models
from django.contrib.auth.models import User


class Role(models.Model):
    name = models.CharField(max_length=50, unique=True)

    def __str__(self):
        return self.name


class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    role = models.ForeignKey(
        Role, on_delete=models.SET_NULL, null=True, related_name="users"
    )

    def __str__(self):
        return f"{self.user.username} - {self.role}"
