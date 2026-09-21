"""Bounded screenshot validation without importing the former ledger pipeline."""
import base64
import binascii
import io
import warnings
from PIL import Image
from app.domain.spending import fail

MAX_BYTES = 10 * 1024 * 1024
MAX_ENCODED = ((MAX_BYTES + 2) // 3) * 4


def decode_image(payload):
    encoded, mime = payload.get("base64"), payload.get("mime_type")
    if not isinstance(encoded, str) or not encoded or len(encoded) > MAX_ENCODED:
        fail("INVALID_IMAGE_PAYLOAD", "The screenshot is empty or exceeds 10 MiB.")
    if mime not in ("image/jpeg", "image/png"):
        fail("INVALID_IMAGE_PAYLOAD", "Only JPEG and PNG screenshots are supported.")
    try:
        raw = base64.b64decode(encoded, validate=True)
        if not raw or len(raw) > MAX_BYTES:
            raise ValueError()
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if image.width * image.height > 20_000_000:
                    raise ValueError()
                if image.format != {"image/jpeg": "JPEG", "image/png": "PNG"}[mime]:
                    raise ValueError()
                image.verify()
    except (ValueError, OSError, binascii.Error, Image.DecompressionBombError, Image.DecompressionBombWarning):
        fail("INVALID_IMAGE_PAYLOAD", "The screenshot is invalid, corrupt or too large.")
    return raw, mime
