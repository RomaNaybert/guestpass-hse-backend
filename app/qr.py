from io import BytesIO

import cv2
import numpy as np
import qrcode
from PIL import Image, UnidentifiedImageError

from app.security import InvalidToken

MAX_IMAGE_BYTES = 10 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 20_000_000


def make_qr(token: str) -> bytes:
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=12, border=4)
    qr.add_data(token)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def read_qr(data: bytes) -> str:
    if len(data) > MAX_IMAGE_BYTES:
        raise InvalidToken("Картинка слишком большая. Отправьте файл до 10 МБ")
    try:
        with Image.open(BytesIO(data)) as image:
            if image.width * image.height > 20_000_000:
                raise InvalidToken("Слишком большое разрешение изображения")
            image.thumbnail((1800, 1800))
            rgb = np.array(image.convert("RGB"))
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise InvalidToken("Не удалось прочитать картинку") from exc
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    detector = cv2.QRCodeDetector()
    found, decoded, _, _ = detector.detectAndDecodeMulti(gray)
    codes = [x for x in decoded if x] if found else []
    if not codes:
        value, _, _ = detector.detectAndDecode(gray)
        codes = [value] if value else []
    if len(codes) > 1:
        raise InvalidToken("На фото несколько QR. Снимите один пропуск крупнее")
    if not codes:
        raise InvalidToken("QR не распознан. Снимите его крупнее или отправьте код текстом")
    return codes[0]
