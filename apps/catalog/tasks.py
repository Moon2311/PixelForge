"""Catalog background tasks (consumer group ``images``)."""

from apps.events.registry import task

from . import images


# Image work can take a while on large uploads.
@task("images", "image.uploaded", timeout=120.0)
def process_uploaded_image(event):
    images.process(event.payload["path"])
