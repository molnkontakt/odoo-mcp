"""Attachment image tools: model allowlist, type allowlist, downscaling, PDF paging."""

from __future__ import annotations

import base64
import io
from unittest.mock import MagicMock

import pytest

from odoo_mcp.tools import attachments as att


@pytest.fixture
def client(monkeypatch):
    c = MagicMock()
    monkeypatch.setattr(att, "get_client", lambda instance: c)
    return c


def _png(w: int, h: int) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGBA", (w, h), (200, 30, 30, 255)).save(buf, format="PNG")
    return buf.getvalue()


def _pdf(pages: int) -> bytes:
    import pymupdf
    doc = pymupdf.open()
    for i in range(pages):
        pg = doc.new_page(width=595, height=842)
        pg.insert_text((72, 72), f"sida {i + 1}")
    return doc.tobytes()


def _row(raw: bytes, mimetype: str, res_model: str = "account.move", name: str = "kvitto"):
    return [{"id": 9, "name": name, "mimetype": mimetype, "res_model": res_model, "res_id": 5, "file_size": len(raw),
             "datas": base64.b64encode(raw).decode()}]


def test_list_attachments_flags_viewable_and_refuses_other_models(client):
    client.execute_kw.return_value = [{"id": 1, "name": "a.pdf", "mimetype": "application/pdf", "file_size": 10, "create_date": "x"},
                                      {"id": 2, "name": "b.xlsx", "mimetype": "application/vnd.ms-excel", "file_size": 10, "create_date": "x"}]
    rows = att.odoo_list_attachments(res_model="hr.expense", res_id=3, instance="dev")
    assert [r["viewable"] for r in rows] == [True, False]
    with pytest.raises(ValueError, match="not served"):
        att.odoo_list_attachments(res_model="mail.message", res_id=3, instance="dev")


def test_image_is_downscaled_and_jpeg(client):
    client.execute_kw.return_value = _row(_png(4000, 1000), "image/png")
    img, txt = att.odoo_get_attachment_image(attachment_id=9, max_px=800, instance="dev")
    assert img.mimeType == "image/jpeg" and "kvitto" in txt.text
    from PIL import Image
    with Image.open(io.BytesIO(base64.b64decode(img.data))) as im:
        assert max(im.size) == 800
    assert len(base64.b64decode(img.data)) < 200_000


def test_pdf_pages_render_and_range_checked(client):
    client.execute_kw.return_value = _row(_pdf(3), "application/pdf", name="faktura.pdf")
    img, txt = att.odoo_get_attachment_image(attachment_id=9, page=2, max_px=600, instance="dev")
    assert img.mimeType == "image/png" and "page 2 of 3" in txt.text
    with pytest.raises(ValueError, match="out of range"):
        att.odoo_get_attachment_image(attachment_id=9, page=4, instance="dev")


def test_refuses_wrong_model_type_and_size(client):
    client.execute_kw.return_value = _row(_png(10, 10), "image/png", res_model="res.users")
    with pytest.raises(ValueError, match="does not serve"):
        att.odoo_get_attachment_image(attachment_id=9, instance="dev")
    client.execute_kw.return_value = _row(b"xlsx", "application/vnd.ms-excel")
    with pytest.raises(ValueError, match="only images and PDF"):
        att.odoo_get_attachment_image(attachment_id=9, instance="dev")
    big = _row(_png(10, 10), "image/png")
    big[0]["file_size"] = att.MAX_RAW_BYTES + 1
    client.execute_kw.return_value = big
    with pytest.raises(ValueError, match="too large"):
        att.odoo_get_attachment_image(attachment_id=9, instance="dev")


def test_max_px_is_clamped():
    assert att._clamp_px(50) == 200 and att._clamp_px(99999) == att.MAX_PX_CEILING and att._clamp_px(0) == 1600
