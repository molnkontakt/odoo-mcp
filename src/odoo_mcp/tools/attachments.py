"""Show document attachments (receipts, supplier invoices) as images.

The read policy strips ``datas``/``raw``/``access_token`` from every generic
``ir.attachment`` read so nobody can bulk-dump documents. This module is the one
deliberate opening: *one* attachment at a time, by id, only image/PDF payloads,
only attachments hanging off accounting/expense records, returned as a
downscaled PNG/JPEG the client can render inline. PDFs are rasterised one page
per call. Odoo's own ACL still decides whether the caller may read the record
(with act-as-caller, that is the human's rights).

Optional dependency group ``images`` (Pillow + PyMuPDF); without it the tools
report that image support is not installed instead of failing obscurely.
"""

from __future__ import annotations

import base64
import io
from typing import Any

from mcp.types import ImageContent, TextContent

from odoo_mcp.app import mcp
from odoo_mcp.auth import SCOPE_READ, requires_scope
from odoo_mcp.client import enter_company_scope, get_client
from odoo_mcp.instances import Instance

#: Records whose attachments may be shown. Anything else (mail, users, config)
#: is refused even if the Odoo user could read it.
ATTACHMENT_MODEL_PREFIXES: tuple[str, ...] = ("account.", "hr.expense", "product.")
ATTACHMENT_MODELS: frozenset[str] = frozenset({"res.partner"})
IMAGE_TYPES: frozenset[str] = frozenset({"image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp", "image/bmp", "image/tiff"})
PDF_TYPES: frozenset[str] = frozenset({"application/pdf", "application/x-pdf"})
MAX_RAW_BYTES = 25 * 1024 * 1024
MAX_PX_CEILING = 2400


def _model_allowed(res_model: str | None) -> bool:
    if not res_model:
        return False
    return res_model in ATTACHMENT_MODELS or any(res_model.startswith(p) for p in ATTACHMENT_MODEL_PREFIXES)


def _clamp_px(max_px: int) -> int:
    return max(200, min(int(max_px or 1600), MAX_PX_CEILING))


def _render_raster(raw: bytes, max_px: int) -> tuple[bytes, str]:
    try:
        from PIL import Image as PILImage
    except ImportError as e:  # pragma: no cover - environment dependent
        raise RuntimeError("image support is not installed (pip install 'molnkontakt-odoo-mcp[images]')") from e
    with PILImage.open(io.BytesIO(raw)) as im:
        im.load()
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        im.thumbnail((max_px, max_px))
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=85, optimize=True)
        return out.getvalue(), "jpeg"


def _render_pdf_page(raw: bytes, page: int, max_px: int) -> tuple[bytes, str, int]:
    try:
        import pymupdf
    except ImportError as e:  # pragma: no cover - environment dependent
        raise RuntimeError("PDF support is not installed (pip install 'molnkontakt-odoo-mcp[images]')") from e
    with pymupdf.open(stream=raw, filetype="pdf") as doc:
        n = doc.page_count
        if page < 1 or page > n:
            raise ValueError(f"page {page} out of range (document has {n} page(s))")
        pg = doc[page - 1]
        rect = pg.rect
        zoom = max_px / max(rect.width, rect.height)
        pix = pg.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        return pix.tobytes("png"), "png", n


@mcp.tool()
@requires_scope(SCOPE_READ)
def odoo_list_attachments(
    res_model: str,
    res_id: int,
    instance: Instance | None = None,
) -> list[dict[str, Any]]:
    """List the files attached to an accounting/expense record (no content).

    Use the returned `attachment_id` with `odoo_get_attachment_image` to see a
    receipt or supplier invoice. Only records under account.*, hr.expense*,
    product.* and res.partner are served.

    Args:
        res_model: e.g. "account.move", "hr.expense"
        res_id: record id
        instance: instance name; may be omitted when only one is configured
    """
    if not _model_allowed(res_model):
        raise ValueError(f"Attachments on {res_model!r} are not served by this tool.")
    client = get_client(instance)
    rows = client.execute_kw(
        "ir.attachment", "search_read",
        [[("res_model", "=", res_model), ("res_id", "=", int(res_id))]],
        {"fields": ["id", "name", "mimetype", "file_size", "create_date"], "order": "id"},
    )
    out = []
    for r in rows:
        mt = (r.get("mimetype") or "").lower()
        out.append({
            "attachment_id": r["id"], "name": r.get("name"), "mimetype": mt,
            "file_size": r.get("file_size"), "created": r.get("create_date"),
            "viewable": mt in IMAGE_TYPES or mt in PDF_TYPES,
        })
    return out


@mcp.tool()
@requires_scope(SCOPE_READ)
def odoo_get_attachment_image(
    attachment_id: int,
    page: int = 1,
    max_px: int = 1600,
    company_id: int | None = None,
    include_caption: bool = False,
    instance: Instance | None = None,
) -> list[ImageContent | TextContent]:
    """Return one attachment as an inline image (receipt, scanned invoice, PDF page).

    Images are downscaled to `max_px` on the long side and re-encoded as JPEG;
    PDFs are rendered one page per call as PNG (`page` is 1-based; the block's
    `_meta.pages` — or the caption with `include_caption=True` — tells how many
    pages there are). Only image/* and PDF
    attachments on accounting/expense records are served; the caller's Odoo
    rights decide whether the record is visible at all.

    Args:
        attachment_id: from `odoo_list_attachments` or an invoice's `message_main_attachment_id`
        page: PDF page to render (default 1)
        max_px: long-side limit in pixels (200–2400, default 1600)
        company_id: optional company scope
        include_caption: also return the caption as a text block (default False: image only,
            which is what most clients need to render the picture inline)
        instance: instance name; may be omitted when only one is configured
    """
    client = get_client(instance)
    enter_company_scope(company_id)
    rows = client.execute_kw(
        "ir.attachment", "read", [[int(attachment_id)]],
        {"fields": ["name", "mimetype", "res_model", "res_id", "file_size", "datas"]},
    )
    if not rows:
        raise ValueError(f"Attachment {attachment_id} not found or not accessible.")
    att = rows[0]
    if not _model_allowed(att.get("res_model")):
        raise ValueError(f"Attachment {attachment_id} belongs to {att.get('res_model')!r}, which this tool does not serve.")
    mt = (att.get("mimetype") or "").lower()
    if mt not in IMAGE_TYPES and mt not in PDF_TYPES:
        raise ValueError(f"Attachment {attachment_id} is {mt or 'of unknown type'}; only images and PDF can be shown.")
    if (att.get("file_size") or 0) > MAX_RAW_BYTES:
        raise ValueError(f"Attachment {attachment_id} is too large to render ({att['file_size']} bytes).")
    raw = base64.b64decode(att["datas"] or b"")
    if not raw:
        raise ValueError(f"Attachment {attachment_id} has no content stored in Odoo.")
    px = _clamp_px(max_px)
    pages = 1
    if mt in PDF_TYPES:
        data, fmt, pages = _render_pdf_page(raw, int(page), px)
        caption = f"{att['name']} — page {int(page)} of {pages} (attachment {attachment_id}, {att['res_model']} {att['res_id']})"
    else:
        data, fmt = _render_raster(raw, px)
        caption = f"{att['name']} (attachment {attachment_id}, {att['res_model']} {att['res_id']})"
    # A single image block: clients render an image-only result inline, but several
    # (Claude Desktop among them) fall back to the text view as soon as a text block
    # sits next to it, leaving the picture visible only to the model. The caption
    # therefore travels in the block's metadata, and `include_caption=True` adds it
    # as a separate text block for clients that prefer that.
    image = ImageContent(
        type="image", data=base64.b64encode(data).decode("ascii"), mimeType=f"image/{fmt}",
        _meta={"caption": caption, "attachment_id": int(attachment_id), "page": int(page) if mt in PDF_TYPES else None,
               "pages": pages if mt in PDF_TYPES else 1, "res_model": att["res_model"], "res_id": att["res_id"]},
    )
    if include_caption:
        return [image, TextContent(type="text", text=caption)]
    return [image]
