from io import BytesIO
from uuid import uuid4

import pytest
from PIL import Image

from app.qr import make_qr, read_qr
from app.security import InvalidToken, PassClaims, issue_pass


def test_real_pass_png_is_decoded():
    token = issue_pass(PassClaims(uuid4(), uuid4(), uuid4(), 9999999999), "x" * 32)
    assert read_qr(make_qr(token)) == token


def test_jpeg_photo_is_decoded():
    token = issue_pass(PassClaims(uuid4(), uuid4(), uuid4(), 9999999999), "x" * 32)
    image = Image.open(BytesIO(make_qr(token))).convert("RGB")
    buffer = BytesIO()
    image.save(buffer, "JPEG", quality=70)
    assert read_qr(buffer.getvalue()) == token


def test_invalid_image_reports_error():
    with pytest.raises(InvalidToken):
        read_qr(b"not an image")


def test_image_without_qr_reports_error():
    buffer = BytesIO()
    Image.new("RGB", (400, 400), "white").save(buffer, "PNG")
    with pytest.raises(InvalidToken, match="не распознан"):
        read_qr(buffer.getvalue())


def test_oversized_file_rejected():
    with pytest.raises(InvalidToken, match="большая"):
        read_qr(b"x" * (10 * 1024 * 1024 + 1))
