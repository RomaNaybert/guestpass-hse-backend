from io import BytesIO

import zxingcpp
import qrcode
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError
from pathlib import Path

from app.security import InvalidToken

MAX_IMAGE_BYTES = 10 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 20_000_000


def make_qr(token: str, box_size: int = 12) -> bytes:
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=box_size, border=4)
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
            codes = [barcode.text for barcode in zxingcpp.read_barcodes(image.convert("RGB"), formats=zxingcpp.BarcodeFormat.QRCode)]
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise InvalidToken("Не удалось прочитать картинку") from exc
    if len(codes) > 1:
        raise InvalidToken("На фото несколько QR. Снимите один пропуск крупнее")
    if not codes:
        raise InvalidToken("QR не распознан. Снимите его крупнее или отправьте код текстом")
    return codes[0]


def make_pass_image(token: str, name: str, event_name: str) -> bytes:
    """Render a real QR over the generated template; keep a clear paper quiet zone around the dark modules."""
    static = Path(__file__).parent / "static"
    with Image.open(static / "assets/pass-template.png") as template:
        ticket = ImageOps.fit(template.convert("RGB"), (1080, 1440))
    with Image.open(BytesIO(make_qr(token, box_size=14))) as qr:
        # Only dark modules are printed. The light template remains visible, including the quiet zone.
        mask = ImageOps.invert(qr.convert("L"))
        ticket.paste("#103c32", ((1080 - qr.width) // 2, 395, (1080 + qr.width) // 2, 395 + qr.height), mask)
    draw = ImageDraw.Draw(ticket)
    font_path = str(static / "fonts/Manrope.ttf")

    def centered(text, y, size):
        text = " ".join(text.split())[:100]
        font = ImageFont.truetype(font_path, size)
        font.set_variation_by_axes([650 if size >= 40 else 500])
        while font.getlength(text) > 880 and size > 22:
            size -= 2
            font = ImageFont.truetype(font_path, size)
        if font.getlength(text) > 880:
            while font.getlength(text + "…") > 880:
                text = text[:-1]
            text += "…"
        draw.text((540, y), text, anchor="mt", font=font, fill="#103c32")

    centered(event_name, 345, 32)
    centered(name, 1210, 46)
    out = BytesIO()
    ticket.save(out, format="PNG")
    return out.getvalue()
