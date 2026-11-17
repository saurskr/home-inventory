"""Receipt reading with a vision model: classify an image, extract line items, clean the result.

Standard library only (Python 3.9+). Independent of any app: pass in a provider
(see providers.py) and an image, get plain dicts back.

    from receipt_module import OllamaProvider, classify_image, extract_receipt
    p = OllamaProvider("http://localhost:11434", "qwen2.5vl:7b")
    classify_image(p, open("photo.jpg", "rb").read())        # -> "receipt" | "item" | "other"
    extract_receipt(p, open("receipt.jpg", "rb").read())     # -> {"store", "date", "items": [...], ...}
"""
import base64
import datetime
import json
import re

from .providers import ProviderError  # noqa: F401  (re-exported for callers)

KINDS = ("receipt", "item", "other")
DEFAULT_CATEGORIES = ["Appliance", "Furniture", "Electronics", "Clothing", "Media", "Collectible",
                      "Consumable", "Service", "Other"]
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_ITEMS = 200


class ReceiptError(Exception):
    """Bad input (unreadable image) or a model answer that could not be used."""

    def __init__(self, message, kind="bad_output"):
        super().__init__(message)
        self.message, self.kind = message, kind


# ---------- images ----------

_MAGIC = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"),
          (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"))
_ALLOWED = {"image/png", "image/jpeg", "image/webp", "image/gif"}


def load_image(image):
    """Accept bytes, a data URL, or a base64 string. Returns (media_type, base64_text)."""
    media = None
    if isinstance(image, (bytes, bytearray)):
        raw = bytes(image)
    elif isinstance(image, str):
        m = re.match(r"^data:([\w/+.-]+);base64,(.*)$", image, re.S)
        text = m.group(2) if m else image
        media = m.group(1) if m else None
        try:
            raw = base64.b64decode(text, validate=True)
        except ValueError:
            raise ReceiptError("The image data is not valid base64.", "bad_image")
    else:
        raise ReceiptError("Image must be bytes, a data URL or base64 text.", "bad_image")
    if not raw:
        raise ReceiptError("The image is empty.", "bad_image")
    if len(raw) > MAX_IMAGE_BYTES:
        raise ReceiptError("The image is larger than %d MB." % (MAX_IMAGE_BYTES // 2**20), "bad_image")
    for magic, mt in _MAGIC:
        if raw.startswith(magic):
            media = mt
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        media = "image/webp"
    if media not in _ALLOWED:
        raise ReceiptError("Unsupported image type. Use PNG, JPEG, WebP or GIF.", "bad_image")
    return media, base64.b64encode(raw).decode()


# ---------- model output ----------

def parse_json_loose(content):
    """Dict from a model answer: accepts a dict, JSON text, or JSON wrapped in chatter."""
    if isinstance(content, dict):
        return content
    if not isinstance(content, str):
        return None
    try:
        out = json.loads(content)
    except ValueError:
        m = re.search(r"\{.*\}", content, re.S)
        try:
            out = json.loads(m.group(0)) if m else None
        except ValueError:
            out = None
    return out if isinstance(out, dict) else None


# ---------- classify ----------

CLASSIFY_SCHEMA = {"type": "object", "properties": {"kind": {"type": "string", "enum": list(KINDS)}},
                   "required": ["kind"]}
CLASSIFY_PROMPT = (
    "Look at this image and decide what it is.\n"
    "- receipt: a store receipt, invoice, order confirmation or packing slip that lists purchased "
    "things with prices (paper or screenshot).\n"
    "- item: a photo of a physical object, such as an appliance, furniture or electronics.\n"
    "- other: anything else (a person, a landscape, a blank page, a document with no purchases).\n"
    "Answer with the kind only.")


def classify_image(provider, image):
    """Returns 'receipt', 'item' or 'other'."""
    media, b64 = load_image(image)
    raw = provider.chat_json(CLASSIFY_PROMPT, CLASSIFY_SCHEMA, b64, media)
    d = parse_json_loose(raw) or {}
    kind = str(d.get("kind", "")).strip().lower()
    return kind if kind in KINDS else "other"


# ---------- extract ----------

def receipt_schema(categories):
    line = {"type": "object", "properties": {
        "description": {"type": "string"}, "quantity": {"type": "number"},
        "unit_price": {"type": "number"}, "total_price": {"type": "number"},
        "category": {"type": "string", "enum": list(categories)}},
        "required": ["description", "quantity", "unit_price", "total_price", "category"]}
    return {"type": "object", "properties": {
        "store": {"type": "string"}, "date": {"type": "string"}, "currency": {"type": "string"},
        "subtotal": {"type": "number"}, "tax": {"type": "number"}, "total": {"type": "number"},
        "line_items": {"type": "array", "items": line}},
        "required": ["store", "date", "currency", "subtotal", "tax", "total", "line_items"]}


def receipt_prompt(categories):
    return (
        "This is a purchase receipt. Read it carefully and list every purchased line item.\n"
        "- store: the merchant name as printed.\n"
        "- date: the purchase date as printed (any format), or '' if not visible.\n"
        "- currency: ISO code such as USD (use USD if you see $ and nothing says otherwise).\n"
        "- subtotal, tax, total: the printed amounts, or 0 if not shown.\n"
        "- line_items: one entry per purchased product. Give the product name in plain words "
        "(expand abbreviations when you can). quantity is how many were bought (1 if not shown). "
        "unit_price is the price of one, total_price the line total, 0 if not shown. "
        "Do NOT include subtotal, tax, total, change, payment or discount lines as items.\n"
        "- category: one of " + ", ".join(categories) + ". Use Consumable for food, toiletries and "
        "other things used up; Service for labour, delivery or fees.\n"
        "Copy numbers exactly. Do not invent items you cannot read.")


_NON_ITEM = re.compile(
    r"^\W*(sub\s*-?\s*total|total|tax|sales\s+tax|balance|amount\s+due|change|cash|visa|mastercard|"
    r"amex|discover|debit|credit|card|tender|payment|thank\s+you|savings|you\s+saved|rounding)\b", re.I)


def to_money(v):
    """Number from 12.5, '$1,299.00', '12,99' ... or None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return round(float(v), 2) if v == v and abs(v) != float("inf") else None
    if not isinstance(v, str):
        return None
    s = re.sub(r"[^\d.,\-()]", "", v.strip())
    if not s or not re.search(r"\d", s):
        return None
    neg = s.startswith("-") or (s.startswith("(") and s.endswith(")"))
    s = s.strip("-()")
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".") if re.search(r",\d{1,2}$", s) else s.replace(",", "")
    try:
        f = round(float(s), 2)
    except ValueError:
        return None
    return -f if neg else f


def to_qty(v):
    q = to_money(v)
    return max(1, min(999, int(round(q)))) if q and q > 0 else 1


_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y", "%b %d, %Y", "%B %d, %Y",
                 "%d %b %Y", "%d %B %Y", "%b %d %Y", "%Y/%m/%d", "%d.%m.%Y")


def normalize_date(v):
    """YYYY-MM-DD, or '' when the text is not a recognisable date."""
    if not isinstance(v, str):
        return ""
    s = re.sub(r"\s+", " ", v.strip())
    s = re.split(r"\s+\d{1,2}:\d{2}", s)[0].strip()  # drop a trailing time
    for f in _DATE_FORMATS:
        try:
            d = datetime.datetime.strptime(s, f).date()
        except ValueError:
            continue
        if 1990 <= d.year <= datetime.date.today().year + 1:
            return d.isoformat()
    return ""


def normalize_receipt(raw, categories=None):
    """Clean a model's raw receipt answer into the stable shape documented in README.md."""
    categories = list(categories or DEFAULT_CATEGORIES)
    raw = parse_json_loose(raw)
    if raw is None:
        raise ReceiptError("The model did not return a usable receipt. Try again with a clearer, "
                           "closer photo.")
    warnings, items, adjustments = [], [], []
    lines = raw.get("line_items")
    for ln in (lines if isinstance(lines, list) else [])[:MAX_ITEMS]:
        if not isinstance(ln, dict):
            continue
        desc = re.sub(r"\s+", " ", str(ln.get("description") or "")).strip()[:160]
        if not desc or _NON_ITEM.match(desc):
            continue
        qty, unit, total = to_qty(ln.get("quantity")), to_money(ln.get("unit_price")), to_money(ln.get("total_price"))
        unit = unit if unit else None
        total = total if total else None
        if total is None and unit is not None:
            total = round(unit * qty, 2)
        elif unit is None and total is not None:
            unit = round(total / qty, 2)
        cat = str(ln.get("category") or "").strip()
        cat = next((c for c in categories if c.lower() == cat.lower()), "Other" if "Other" in categories else categories[-1])
        row = {"description": desc, "quantity": qty, "unit_price": unit, "total_price": total, "category": cat}
        if total is not None and total < 0:
            adjustments.append(row)          # discount or coupon line
        else:
            items.append(row)
    out = {"store": re.sub(r"\s+", " ", str(raw.get("store") or "")).strip()[:80],
           "date": normalize_date(raw.get("date")),
           "currency": (str(raw.get("currency") or "USD").strip().upper()[:3] or "USD"),
           "subtotal": to_money(raw.get("subtotal")) or None, "tax": to_money(raw.get("tax")) or None,
           "total": to_money(raw.get("total")) or None, "items": items, "adjustments": adjustments,
           "warnings": warnings}
    if not items:
        warnings.append("No line items could be read from this receipt.")
    else:
        got = round(sum(i["total_price"] or 0 for i in items) + sum(a["total_price"] or 0 for a in adjustments), 2)
        want = out["subtotal"] if out["subtotal"] else (round(out["total"] - (out["tax"] or 0), 2) if out["total"] else None)
        if want and abs(got - want) > max(1.0, 0.01 * want):
            warnings.append("The items add up to %.2f but the receipt shows %.2f. Some lines may be missing "
                            "or misread; check them." % (got, want))
        if any(i["total_price"] is None for i in items):
            warnings.append("Some line prices could not be read.")
    if raw.get("date") and not out["date"]:
        warnings.append("The purchase date could not be understood.")
    return out


def extract_receipt(provider, image, categories=None):
    """Read a receipt image. Returns {store, date, currency, subtotal, tax, total, items, adjustments, warnings}."""
    categories = list(categories or DEFAULT_CATEGORIES)
    media, b64 = load_image(image)
    raw = provider.chat_json(receipt_prompt(categories), receipt_schema(categories), b64, media)
    return normalize_receipt(raw, categories)
