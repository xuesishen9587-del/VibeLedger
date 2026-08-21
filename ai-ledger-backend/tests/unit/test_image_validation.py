import unittest
import base64
import io
from PIL import Image
from app.services.expense_service import validate_image_payload
from app.domain.transactions import InvalidImagePayloadError

def _create_minimal_png_b64() -> str:
    buf = io.BytesIO()
    img = Image.new("RGB", (10, 10), color="red")
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")

def _create_minimal_jpeg_b64() -> str:
    buf = io.BytesIO()
    img = Image.new("RGB", (10, 10), color="blue")
    img.save(buf, format="JPEG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")

class TestImageValidation(unittest.TestCase):
    def test_valid_jpeg_and_png_payloads(self):
        png_b64 = _create_minimal_png_b64()
        img_bytes, mime = validate_image_payload({"base64": png_b64, "mime_type": "image/png"})
        self.assertEqual(mime, "image/png")
        self.assertTrue(len(img_bytes) > 0)

        jpeg_b64 = _create_minimal_jpeg_b64()
        img_bytes, mime = validate_image_payload({"base64": jpeg_b64, "mime_type": "image/jpeg"})
        self.assertEqual(mime, "image/jpeg")
        self.assertTrue(len(img_bytes) > 0)

    def test_malformed_base64_rejected(self):
        with self.assertRaises(InvalidImagePayloadError):
            validate_image_payload({"base64": "not-valid-base64!@#$", "mime_type": "image/png"})

    def test_fake_bytes_or_mime_mismatch_rejected(self):
        # Fake bytes pretending to be JPEG
        fake_b64 = base64.b64encode(b"not_an_image_file").decode("utf-8")
        with self.assertRaises(InvalidImagePayloadError):
            validate_image_payload({"base64": fake_b64, "mime_type": "image/jpeg"})

        # Valid PNG declared as JPEG
        png_b64 = _create_minimal_png_b64()
        with self.assertRaises(InvalidImagePayloadError):
            validate_image_payload({"base64": png_b64, "mime_type": "image/jpeg"})

    def test_oversized_image_rejected(self):
        png_b64 = _create_minimal_png_b64()
        # Set artificial limit of 10 bytes
        with self.assertRaises(InvalidImagePayloadError) as ctx:
            validate_image_payload({"base64": png_b64, "mime_type": "image/png"}, max_bytes=10)
        self.assertIn("exceeds maximum limit", str(ctx.exception))

    def test_corrupted_payload_with_valid_magic_bytes_rejected(self):
        # Starts with PNG magic header but truncated/corrupted body
        corrupted_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
        corrupted_b64 = base64.b64encode(corrupted_bytes).decode("utf-8")
        with self.assertRaises(InvalidImagePayloadError):
            validate_image_payload({"base64": corrupted_b64, "mime_type": "image/png"})
