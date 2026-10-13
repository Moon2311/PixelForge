"""Background product image optimisation (consumer group ``images``).

An admin upload stores the original file and queues ``image.uploaded``; the
request doesn't wait for any image work. The worker writes resized WebP
variants next to the original (``<name>.w320.webp``, ``<name>.w800.webp``,
``<name>.w1600.webp``; never larger than the original) without metadata
(EXIF, GPS), and records them in ``ProcessedImage``.

Idempotent: variant names are derived from the source, files are written
to a temporary name and renamed into place, and a ``ProcessedImage`` row
means the job is done.

The media directory must be shared by the web instances and the image
workers (one volume in Docker Compose, object storage in production).
"""

import os
import tempfile
from pathlib import Path

from django.conf import settings
from django.db import IntegrityError, transaction
from PIL import Image, ImageOps, UnidentifiedImageError

from apps.events import outbox, topics
from apps.events.retry import NonRetryableError

from .models import ProcessedImage

WIDTHS = (320, 800, 1600)
WEBP_QUALITY = 82
# Refuse decompression bombs well below Pillow's own warning threshold.
MAX_PIXELS = 50_000_000


def queue_processing(relative_path):
    """Queue an uploaded file (path relative to MEDIA_ROOT) for processing."""
    with transaction.atomic():
        outbox.enqueue(
            "image.uploaded", topic=topics.IMAGE_PROCESSING, aggregate_type="image",
            aggregate_id=relative_path, payload={"path": relative_path},
        )


def _source(relative_path):
    root = Path(settings.MEDIA_ROOT).resolve()
    path = (root / relative_path).resolve()
    if root not in path.parents:
        raise NonRetryableError("image path outside MEDIA_ROOT")
    return root, path


def variant_path(relative_path, width):
    stem, _ = os.path.splitext(relative_path)
    return f"{stem}.w{width}.webp"


def process(relative_path):
    if ProcessedImage.objects.filter(source_path=relative_path).exists():
        return None
    root, path = _source(relative_path)
    if not path.exists():
        # Missing everywhere: the upload's request failed after queueing, or
        # the volume isn't shared (a configuration error worth retrying).
        raise FileNotFoundError(f"{relative_path} not found under MEDIA_ROOT")

    try:
        with Image.open(path) as image:
            if image.width * image.height > MAX_PIXELS:
                return _record(relative_path, ProcessedImage.STATUS_SKIPPED, path, image.size, {},
                               note="image too large to process")
            image = ImageOps.exif_transpose(image)
            image.load()
            size = image.size
            transparent = image.mode in ("RGBA", "LA") or "transparency" in image.info
            image = image.convert("RGBA" if transparent else "RGB")
            variants = {}
            for width in WIDTHS:
                if width > size[0] and variants:
                    break  # don't upscale; the smallest variant is always made
                target = min(width, size[0])
                height = max(1, round(size[1] * target / size[0]))
                resized = image.resize((target, height), Image.Resampling.LANCZOS)
                name = variant_path(relative_path, width)
                _save_atomic(resized, root / name)
                variants[f"w{width}"] = {"path": name, "width": target, "height": height,
                                         "bytes": (root / name).stat().st_size}
    except (UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise NonRetryableError(f"not a processable image: {type(exc).__name__}") from None
    return _record(relative_path, ProcessedImage.STATUS_DONE, path, size, variants)


def _save_atomic(image, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=destination.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            # No exif/icc arguments: the variant carries no metadata.
            image.save(handle, "WEBP", quality=WEBP_QUALITY, method=4)
        os.replace(temporary, destination)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def _record(relative_path, status, path, size, variants, note=""):
    try:
        with transaction.atomic():
            return ProcessedImage.objects.create(
                source_path=relative_path, status=status, width=size[0], height=size[1],
                original_bytes=path.stat().st_size, variants=variants, note=note,
            )
    except IntegrityError:
        return ProcessedImage.objects.get(source_path=relative_path)
