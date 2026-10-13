from django.apps import AppConfig


class EventsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.events"
    verbose_name = "Events"

    def ready(self):
        import atexit

        from django.utils.module_loading import autodiscover_modules

        from apps.events import kafka

        # Registers every app's @task functions (apps/<app>/tasks.py).
        autodiscover_modules("tasks")
        # Deliver queued best-effort audit events on a clean exit.
        atexit.register(kafka.web_producer.flush)
