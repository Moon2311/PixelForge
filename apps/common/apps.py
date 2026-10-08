from django.apps import AppConfig


class CommonConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.common"
    verbose_name = "Common"

    def ready(self):
        from apps.common import checks  # noqa: F401  (registers system checks)
        from apps.common.db import cluster, router  # noqa: F401  (connection signal handlers)
