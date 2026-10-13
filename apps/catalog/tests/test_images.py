import io
import shutil
import tempfile
from pathlib import Path

from django.test import TestCase, override_settings
from PIL import Image

from apps.catalog import images
from apps.catalog.models import ProcessedImage
from apps.events.models import OutboxEvent
from apps.events.retry import NonRetryableError


class ImageProcessingTest(TestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media)
        override = override_settings(MEDIA_ROOT=self.media)
        override.enable()
        self.addCleanup(override.disable)

    def write(self, name, size=(1200, 600), fmt="JPEG"):
        path = Path(self.media) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGB", size, (200, 30, 30))
        exif = Image.Exif()
        exif[0x010F] = "Camera maker"  # metadata that must not reach the variants
        image.save(path, fmt, exif=exif)
        return name

    def test_creates_resized_variants_without_metadata(self):
        name = self.write("products/a.jpg")
        record = images.process(name)
        self.assertEqual((record.status, record.width, record.height), ("done", 1200, 600))
        self.assertEqual(sorted(record.variants), ["w320", "w800"])  # never upscaled past the original
        variant = Path(self.media) / record.variants["w320"]["path"]
        with Image.open(variant) as image:
            self.assertEqual((image.format, image.size), ("WEBP", (320, 160)))
            self.assertFalse(image.getexif())
        self.assertTrue((Path(self.media) / name).exists())  # original kept

    def test_small_image_gets_one_variant(self):
        record = images.process(self.write("products/tiny.png", size=(100, 50), fmt="PNG"))
        self.assertEqual(list(record.variants), ["w320"])
        self.assertEqual(record.variants["w320"]["width"], 100)

    def test_idempotent(self):
        name = self.write("products/b.jpg")
        first = images.process(name)
        self.assertIsNone(images.process(name))  # already processed
        self.assertEqual(ProcessedImage.objects.get().pk, first.pk)

    def test_bad_files(self):
        (Path(self.media) / "products").mkdir()
        (Path(self.media) / "products/x.jpg").write_bytes(b"not an image")
        with self.assertRaises(NonRetryableError):
            images.process("products/x.jpg")
        with self.assertRaises(NonRetryableError):
            images.process("../../etc/passwd")
        with self.assertRaises(FileNotFoundError):  # retried: the volume may not be mounted yet
            images.process("products/missing.jpg")

    def test_upload_queues_processing(self):
        from django.contrib.auth.models import User
        from django.core.files.uploadedfile import SimpleUploadedFile
        from rest_framework.test import APIClient

        from apps.authentication.models import Role, UserProfile
        from apps.authentication.tokens import issue_access_token

        admin = User.objects.create_user("admin", "admin@example.com", "Admin-pass-123!")
        UserProfile.objects.create(user=admin, role=Role.objects.get_or_create(name="admin")[0])
        buffer = io.BytesIO()
        Image.new("RGB", (900, 900)).save(buffer, "PNG")
        upload = SimpleUploadedFile("shot.png", buffer.getvalue(), content_type="image/png")
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_access_token(admin)}")
        with self.captureOnCommitCallbacks(execute=True):
            resp = api.post("/api/products/", {
                "name": "Phone", "sku": "IMG-1", "price": "10.00", "stock_quantity": 3, "images": upload,
                "brand_name": "Acme", "category_name": "Phones",
            }, format="multipart")
        self.assertIn(resp.status_code, (200, 201), resp.content)
        row = OutboxEvent.objects.get(event_type="image.uploaded")
        self.assertTrue(row.payload["payload"]["path"].startswith("products/"))
        self.assertEqual(ProcessedImage.objects.get().source_path, row.payload["payload"]["path"])
