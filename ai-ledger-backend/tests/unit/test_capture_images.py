import base64
import io
import struct
import unittest
import zlib
from fastapi import HTTPException
from PIL import Image
from app.domain.capture_images import decode_image, MAX_ENCODED


class CaptureImageTest(unittest.TestCase):
    def test_valid_image_and_rejected_corruption_mime_and_size(self):
        out = io.BytesIO()
        Image.new("RGB", (3, 3)).save(out, format="PNG")
        raw = out.getvalue()
        payload = {"mime_type": "image/png", "base64": base64.b64encode(raw).decode()}
        self.assertEqual(decode_image(payload), (raw, "image/png"))
        for bad in ({**payload, "mime_type": "image/jpeg"},
                    {**payload, "base64": "!invalid!"},
                    {**payload, "base64": "A" * (MAX_ENCODED + 1)},
                    {**payload, "base64": base64.b64encode(raw[:30]).decode()}):
            with self.subTest(mime=bad["mime_type"]), self.assertRaises(HTTPException) as error:
                decode_image(bad)
            self.assertEqual(error.exception.status_code, 422)

    def test_small_compressed_payload_with_oversized_dimensions_is_rejected(self):
        def chunk(name, content):
            data = name + content
            return struct.pack(">I", len(content)) + data + struct.pack(">I", zlib.crc32(data))
        raw = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 5000, 5000, 8, 2, 0, 0, 0))
        raw += chunk(b"IDAT", zlib.compress(b"")) + chunk(b"IEND", b"")
        with self.assertRaises(HTTPException) as error:
            decode_image({"mime_type": "image/png", "base64": base64.b64encode(raw).decode()})
        self.assertEqual(error.exception.status_code, 422)
