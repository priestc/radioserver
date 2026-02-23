from __future__ import annotations

import io

import qrcode
import qrcode.image.svg


def make_qr_svg(data: str) -> str:
    """Return an inline SVG string containing a QR code for *data*."""
    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode()
