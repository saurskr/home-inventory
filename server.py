#!/usr/bin/env python3
"""Home Inventory: local-first API + static UI. Standard library only."""
import base64
import datetime
import json
import os
import re
import sqlite3
import socket
import sys
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DATA_DIR = os.environ.get("INVENTORY_DATA_DIR", os.path.join(BASE_DIR, "data"))

ITEM_TYPES = ["Appliance", "Furniture", "Electronics", "Memorabilia",
              "Media", "Collectible", "Clothing"]
SOURCES = ["manual", "template", "vision"]

# Room name -> [(item name, item_type)]
ROOM_TEMPLATES = {
    "Kitchen": [("Toaster", "Appliance"), ("Microwave", "Appliance"),
                ("Refrigerator", "Appliance"), ("Blender", "Appliance"),
                ("Crockpot", "Appliance")],
    "Master Bedroom": [("Bed", "Furniture"), ("Dresser", "Furniture"),
                       ("Nightstand", "Furniture"), ("Nightstand", "Furniture")],
    "Living Room": [("Sofa", "Furniture"), ("Television", "Electronics"),
                    ("Bookcase", "Furniture"), ("Coffee Table", "Furniture")],
    "Dining Room": [("Dining Table", "Furniture"), ("Dining Chair", "Furniture"),
                    ("China Cabinet", "Furniture")],
    "Guest Bedroom": [("Bed", "Furniture"), ("Dresser", "Furniture"),
                      ("Nightstand", "Furniture")],
    "Master Bathroom": [("Hair Dryer", "Appliance"), ("Scale", "Appliance")],
    "Guest Bathroom": [("Hair Dryer", "Appliance")],
    "Garage": [("Lawn Mower", "Appliance"), ("Workbench", "Furniture"),
               ("Tool Set", "Appliance")],
}

DEFAULT_OLLAMA_MODEL = "qwen2.5vl:7b"
DEFAULT_CLAUDE_MODEL = "claude-haiku-5-5"
ANTHROPIC_BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
MAX_CLAUDE_IMAGE = 5 * 1024 * 1024
ALLOWED_HOSTS = re.compile(r"^(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$", re.I)

IMAGE_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp",
               "image/gif": "gif"}
MAX_BODY = 20 * 1024 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS homes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  address TEXT
);
CREATE TABLE IF NOT EXISTS rooms (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  home_id INTEGER NOT NULL REFERENCES homes(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  inv_no TEXT UNIQUE,
  room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  brand TEXT NOT NULL DEFAULT '',
  purchased_value REAL NOT NULL DEFAULT 0,
  replacement_value REAL NOT NULL DEFAULT 0,
  similar_url TEXT NOT NULL DEFAULT '',
  item_type TEXT,
  image TEXT,
  similar_image TEXT,
  price_updated_at TEXT,
  source TEXT NOT NULL DEFAULT 'manual'
);
CREATE TABLE IF NOT EXISTS home_insurance (
  home_id INTEGER PRIMARY KEY REFERENCES homes(id) ON DELETE CASCADE,
  insurer TEXT NOT NULL DEFAULT '',
  policy_number TEXT NOT NULL DEFAULT '',
  dwelling_coverage REAL,
  contents_coverage REAL,
  deductible REAL,
  annual_premium REAL,
  renewal_date TEXT NOT NULL DEFAULT '',
  contact TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_rooms_home ON rooms(home_id);
CREATE INDEX IF NOT EXISTS idx_items_room ON items(room_id);
"""


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------- storage helpers ----------

def images_dir():
    return os.path.join(DATA_DIR, "images")


def connect():
    conn = sqlite3.connect(os.path.join(DATA_DIR, "inventory.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_storage():
    os.makedirs(images_dir(), exist_ok=True)
    conn = connect()
    conn.executescript(SCHEMA)
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(items)")]
    if "price_updated_at" not in cols:  # databases created before this column existed
        conn.execute("ALTER TABLE items ADD COLUMN price_updated_at TEXT")
    conn.commit()
    conn.close()


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rows(cur):
    return [dict(r) for r in cur.fetchall()]


def one(conn, sql, args=()):
    r = conn.execute(sql, args).fetchone()
    return dict(r) if r else None


def remove_image_files(names):
    for n in names:
        if n:
            try:
                os.remove(os.path.join(images_dir(), os.path.basename(n)))
            except OSError:
                pass


def item_images_for(conn, where, args):
    out = []
    for r in conn.execute(
            "SELECT image, similar_image FROM items WHERE " + where, args):
        out += [r["image"], r["similar_image"]]
    return out


# ---------- validation ----------

def text(body, key, required=False, default=""):
    v = body.get(key, None)
    if v is None:
        if required:
            raise ApiError(400, "%s is required" % key)
        return default
    if not isinstance(v, str):
        raise ApiError(400, "%s must be a string" % key)
    v = v.strip()
    if required and not v:
        raise ApiError(400, "%s is required" % key)
    return v


def money(body, key):
    v = body.get(key, None)
    if v is None or v == "":
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise ApiError(400, "%s must be a number" % key)
    try:
        f = float(v)
    except ValueError:
        raise ApiError(400, "%s must be a number" % key)
    if f != f or f < 0 or f == float("inf"):
        raise ApiError(400, "%s must be a non-negative number" % key)
    return round(f, 2)


def url_field(body, key):
    v = text(body, key)
    if v and not re.match(r"^https?://\S+$", v, re.I):
        raise ApiError(400, "%s must start with http:// or https://" % key)
    return v


def check_type(v):
    if v in (None, ""):
        return None
    if v not in ITEM_TYPES:
        raise ApiError(400, "item_type must be one of: " + ", ".join(ITEM_TYPES))
    return v


def get_or_404(conn, table, id_):
    r = one(conn, "SELECT * FROM %s WHERE id = ?" % table, (id_,))
    if not r:
        raise ApiError(404, "%s not found" % table[:-1])
    return r


# ---------- handlers ----------

def meta(ctx):
    return 200, {"item_types": ITEM_TYPES, "sources": SOURCES,
                 "room_templates": {k: [{"name": n, "item_type": t} for n, t in v]
                                    for k, v in ROOM_TEMPLATES.items()}}


HOME_SUMMARY = """
SELECT h.*,
  (SELECT COUNT(*) FROM rooms r WHERE r.home_id = h.id) AS room_count,
  (SELECT COUNT(*) FROM items i JOIN rooms r ON r.id = i.room_id
     WHERE r.home_id = h.id) AS item_count,
  (SELECT COALESCE(SUM(i.purchased_value), 0) FROM items i JOIN rooms r
     ON r.id = i.room_id WHERE r.home_id = h.id) AS total_purchased,
  (SELECT COALESCE(SUM(i.replacement_value), 0) FROM items i JOIN rooms r
     ON r.id = i.room_id WHERE r.home_id = h.id) AS total_replacement
FROM homes h
"""

ROOM_SUMMARY = """
SELECT r.*,
  (SELECT COUNT(*) FROM items i WHERE i.room_id = r.id) AS item_count,
  (SELECT COALESCE(SUM(i.purchased_value), 0) FROM items i
     WHERE i.room_id = r.id) AS total_purchased,
  (SELECT COALESCE(SUM(i.replacement_value), 0) FROM items i
     WHERE i.room_id = r.id) AS total_replacement
FROM rooms r
"""


def list_homes(ctx):
    return 200, rows(ctx.db.execute(HOME_SUMMARY + " ORDER BY h.name COLLATE NOCASE"))


def create_home(ctx):
    b = ctx.body
    cur = ctx.db.execute("INSERT INTO homes (name, address) VALUES (?, ?)",
                         (text(b, "name", True), text(b, "address")))
    ctx.db.commit()
    return 201, one(ctx.db, HOME_SUMMARY + " WHERE h.id = ?", (cur.lastrowid,))


def get_home(ctx, id_):
    r = one(ctx.db, HOME_SUMMARY + " WHERE h.id = ?", (id_,))
    if not r:
        raise ApiError(404, "home not found")
    return 200, r


def update_home(ctx, id_):
    cur = get_or_404(ctx.db, "homes", id_)
    b = ctx.body
    name = text(b, "name", True) if "name" in b else cur["name"]
    address = text(b, "address") if "address" in b else cur["address"]
    ctx.db.execute("UPDATE homes SET name = ?, address = ? WHERE id = ?",
                   (name, address, id_))
    ctx.db.commit()
    return get_home(ctx, id_)


def delete_home(ctx, id_):
    get_or_404(ctx.db, "homes", id_)
    files = item_images_for(
        ctx.db, "room_id IN (SELECT id FROM rooms WHERE home_id = ?)", (id_,))
    ctx.db.execute("DELETE FROM homes WHERE id = ?", (id_,))
    ctx.db.commit()
    remove_image_files(files)
    return 200, {"deleted": id_}


def list_rooms(ctx, home_id):
    get_or_404(ctx.db, "homes", home_id)
    return 200, rows(ctx.db.execute(
        ROOM_SUMMARY + " WHERE r.home_id = ? ORDER BY r.name COLLATE NOCASE",
        (home_id,)))


def create_room(ctx, home_id):
    get_or_404(ctx.db, "homes", home_id)
    b = ctx.body
    name = text(b, "name", True)
    cur = ctx.db.execute(
        "INSERT INTO rooms (home_id, name, description) VALUES (?, ?, ?)",
        (home_id, name, text(b, "description")))
    room_id = cur.lastrowid
    if b.get("prefill"):
        key = next((k for k in ROOM_TEMPLATES if k.lower() == name.lower()), None)
        tmpl = text(b, "template") if b.get("template") else key
        tmpl = next((k for k in ROOM_TEMPLATES
                     if tmpl and k.lower() == tmpl.lower()), None)
        if not tmpl:
            ctx.db.rollback()
            raise ApiError(400, "no room template for '%s'" % name)
        for item_name, item_type in ROOM_TEMPLATES[tmpl]:
            insert_item(ctx.db, room_id, {"name": item_name, "item_type": item_type},
                        source="template")
    ctx.db.commit()
    return 201, one(ctx.db, ROOM_SUMMARY + " WHERE r.id = ?", (room_id,))


def get_room(ctx, id_):
    r = one(ctx.db, ROOM_SUMMARY + " WHERE r.id = ?", (id_,))
    if not r:
        raise ApiError(404, "room not found")
    return 200, r


def update_room(ctx, id_):
    cur = get_or_404(ctx.db, "rooms", id_)
    b = ctx.body
    name = text(b, "name", True) if "name" in b else cur["name"]
    desc = text(b, "description") if "description" in b else cur["description"]
    home_id = cur["home_id"]
    if "home_id" in b:
        if not isinstance(b["home_id"], int) or isinstance(b["home_id"], bool):
            raise ApiError(400, "home_id must be an integer")
        get_or_404(ctx.db, "homes", b["home_id"])
        home_id = b["home_id"]
    ctx.db.execute("UPDATE rooms SET name=?, description=?, home_id=? WHERE id=?",
                   (name, desc, home_id, id_))
    ctx.db.commit()
    return get_room(ctx, id_)


def delete_room(ctx, id_):
    get_or_404(ctx.db, "rooms", id_)
    files = item_images_for(ctx.db, "room_id = ?", (id_,))
    ctx.db.execute("DELETE FROM rooms WHERE id = ?", (id_,))
    ctx.db.commit()
    remove_image_files(files)
    return 200, {"deleted": id_}


def insert_item(conn, room_id, b, source="manual"):
    purchased = money(b, "purchased_value")
    replacement = money(b, "replacement_value")
    # An explicitly entered replacement price counts as a price update.
    stamp = now() if (replacement is not None or b.get("price_confirmed") is True) else None
    purchased = purchased if purchased is not None else 0
    replacement = replacement if replacement is not None else purchased
    src = b.get("source", source)
    if src not in SOURCES:
        raise ApiError(400, "source must be one of: " + ", ".join(SOURCES))
    cur = conn.execute(
        """INSERT INTO items (room_id, name, description, brand, purchased_value,
           replacement_value, similar_url, item_type, source, price_updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (room_id, text(b, "name", True), text(b, "description"), text(b, "brand"),
         purchased, replacement, url_field(b, "similar_url"),
         check_type(b.get("item_type")), src, stamp))
    conn.execute("UPDATE items SET inv_no = ? WHERE id = ?",
                 ("INV-%06d" % cur.lastrowid, cur.lastrowid))
    return cur.lastrowid


def list_items(ctx, room_id):
    get_or_404(ctx.db, "rooms", room_id)
    return 200, rows(ctx.db.execute(
        "SELECT * FROM items WHERE room_id = ? ORDER BY name COLLATE NOCASE, id",
        (room_id,)))


def create_item(ctx, room_id):
    get_or_404(ctx.db, "rooms", room_id)
    new_id = insert_item(ctx.db, room_id, ctx.body)
    ctx.db.commit()
    return 201, get_or_404(ctx.db, "items", new_id)


def get_item(ctx, id_):
    return 200, get_or_404(ctx.db, "items", id_)


def update_item(ctx, id_):
    cur = get_or_404(ctx.db, "items", id_)
    b = ctx.body
    f = dict(cur)
    for k in ("name",):
        if k in b:
            f[k] = text(b, k, True)
    for k in ("description", "brand"):
        if k in b:
            f[k] = text(b, k)
    if "similar_url" in b:
        f["similar_url"] = url_field(b, "similar_url")
    if "item_type" in b:
        f["item_type"] = check_type(b["item_type"])
    if "purchased_value" in b:
        v = money(b, "purchased_value")
        f["purchased_value"] = v if v is not None else 0
    if "replacement_value" in b:
        v = money(b, "replacement_value")
        new = v if v is not None else f["purchased_value"]
        if new != cur["replacement_value"]:
            f["price_updated_at"] = now()
        f["replacement_value"] = new
    if b.get("price_confirmed") is True:
        f["price_updated_at"] = now()
    if "source" in b:
        if b["source"] not in SOURCES:
            raise ApiError(400, "invalid source")
        f["source"] = b["source"]
    if "room_id" in b:
        if not isinstance(b["room_id"], int) or isinstance(b["room_id"], bool):
            raise ApiError(400, "room_id must be an integer")
        get_or_404(ctx.db, "rooms", b["room_id"])
        f["room_id"] = b["room_id"]
    ctx.db.execute(
        """UPDATE items SET name=?, description=?, brand=?, purchased_value=?,
           replacement_value=?, similar_url=?, item_type=?, source=?, room_id=?,
           price_updated_at=? WHERE id=?""",
        (f["name"], f["description"], f["brand"], f["purchased_value"],
         f["replacement_value"], f["similar_url"], f["item_type"], f["source"],
         f["room_id"], f["price_updated_at"], id_))
    ctx.db.commit()
    return 200, get_or_404(ctx.db, "items", id_)


def delete_item(ctx, id_):
    cur = get_or_404(ctx.db, "items", id_)
    ctx.db.execute("DELETE FROM items WHERE id = ?", (id_,))
    ctx.db.commit()
    remove_image_files([cur["image"], cur["similar_image"]])
    return 200, {"deleted": id_}


def upload_image(ctx, id_):
    cur = get_or_404(ctx.db, "items", id_)
    kind = ctx.query.get("kind", ["photo"])[0]
    if kind not in ("photo", "similar"):
        raise ApiError(400, "kind must be photo or similar")
    data = ctx.body.get("data")
    m = re.match(r"^data:([\w/+.-]+);base64,(.*)$", data or "", re.S)
    if not m or m.group(1) not in IMAGE_TYPES:
        raise ApiError(400, "data must be a base64 data URL (png, jpeg, webp, gif)")
    try:
        raw = base64.b64decode(m.group(2), validate=True)
    except ValueError:
        raise ApiError(400, "invalid base64 data")
    if not raw:
        raise ApiError(400, "empty image")
    col = "image" if kind == "photo" else "similar_image"
    fname = "%d-%s-%s.%s" % (id_, kind, uuid.uuid4().hex[:8], IMAGE_TYPES[m.group(1)])
    with open(os.path.join(images_dir(), fname), "wb") as fh:
        fh.write(raw)
    ctx.db.execute("UPDATE items SET %s = ? WHERE id = ?" % col, (fname, id_))
    ctx.db.commit()
    remove_image_files([cur[col]])
    return 200, get_or_404(ctx.db, "items", id_)


INS_TEXT = ["insurer", "policy_number", "contact", "notes"]
INS_MONEY = ["dwelling_coverage", "contents_coverage", "deductible", "annual_premium"]
INS_DEFAULTS = dict({k: "" for k in INS_TEXT}, renewal_date="",
                    **{k: None for k in INS_MONEY})


def get_insurance(ctx, home_id):
    get_or_404(ctx.db, "homes", home_id)
    r = one(ctx.db, "SELECT * FROM home_insurance WHERE home_id = ?", (home_id,))
    if not r:
        return 200, dict(INS_DEFAULTS, home_id=home_id, configured=False, updated_at=None)
    r["configured"] = True
    return 200, r


def put_insurance(ctx, home_id):
    get_or_404(ctx.db, "homes", home_id)
    b = ctx.body
    cur = one(ctx.db, "SELECT * FROM home_insurance WHERE home_id = ?", (home_id,)) \
        or dict(INS_DEFAULTS)
    for k in INS_TEXT:
        if k in b:
            cur[k] = text(b, k)
    for k in INS_MONEY:
        if k in b:
            cur[k] = money(b, k)
    if "renewal_date" in b:
        d = text(b, "renewal_date")
        if d:
            try:
                datetime.datetime.strptime(d, "%Y-%m-%d")
            except ValueError:
                raise ApiError(400, "renewal_date must be YYYY-MM-DD")
        cur["renewal_date"] = d
    ctx.db.execute(
        """INSERT OR REPLACE INTO home_insurance (home_id, insurer, policy_number,
           dwelling_coverage, contents_coverage, deductible, annual_premium,
           renewal_date, contact, notes, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (home_id, cur["insurer"], cur["policy_number"], cur["dwelling_coverage"],
         cur["contents_coverage"], cur["deductible"], cur["annual_premium"],
         cur["renewal_date"], cur["contact"], cur["notes"], now()))
    ctx.db.commit()
    return get_insurance(ctx, home_id)


def delete_insurance(ctx, home_id):
    get_or_404(ctx.db, "homes", home_id)
    ctx.db.execute("DELETE FROM home_insurance WHERE home_id = ?", (home_id,))
    ctx.db.commit()
    return 200, {"deleted": home_id}


# ---------- settings (never returns the API key) ----------

def default_ollama_url():
    u = os.environ.get("OLLAMA_HOST", "http://localhost:11434").strip()
    return u if re.match(r"^https?://", u, re.I) else "http://" + u


def load_settings():
    try:
        with open(os.path.join(DATA_DIR, "settings.json")) as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    key = raw.get("anthropic_key") or ""
    source = "settings" if key else None
    if not key and os.environ.get("ANTHROPIC_API_KEY"):
        key, source = os.environ["ANTHROPIC_API_KEY"].strip(), "env"
    return {"ollama_url": raw.get("ollama_url") or default_ollama_url(),
            "ollama_model": raw.get("ollama_model") or DEFAULT_OLLAMA_MODEL,
            "anthropic_model": raw.get("anthropic_model") or DEFAULT_CLAUDE_MODEL,
            "anthropic_key": key, "key_source": source, "_raw": raw}


def save_settings(raw):
    path = os.path.join(DATA_DIR, "settings.json")
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # key file: owner only
    with os.fdopen(fd, "w") as fh:
        json.dump(raw, fh)
    os.replace(tmp, path)


def public_settings():
    c = load_settings()
    return {"ollama": {"url": c["ollama_url"], "model": c["ollama_model"]},
            "anthropic": {"configured": bool(c["anthropic_key"]), "key_source": c["key_source"],
                          "model": c["anthropic_model"]},
            "defaults": {"ollama_url": default_ollama_url(), "ollama_model": DEFAULT_OLLAMA_MODEL,
                         "anthropic_model": DEFAULT_CLAUDE_MODEL}}


def get_settings(ctx):
    return 200, public_settings()


def put_settings(ctx):
    b = ctx.body
    raw = load_settings()["_raw"]
    if "ollama_url" in b:
        u = text(b, "ollama_url").rstrip("/")
        if u and not re.match(r"^https?://[^\s/]+$", u, re.I):
            raise ApiError(400, "ollama_url must look like http://localhost:11434")
        raw["ollama_url"] = u
    for k in ("ollama_model", "anthropic_model"):
        if k in b:
            v = text(b, k)
            if len(v) > 100 or re.search(r"\s", v):
                raise ApiError(400, "%s is not a valid model name" % k)
            raw[k] = v
    if "anthropic_key" in b:
        v = text(b, "anthropic_key")
        if v and (len(v) < 20 or re.search(r"\s", v)):
            raise ApiError(400, "That does not look like an API key")
        if v:
            raw["anthropic_key"] = v
        else:
            raw.pop("anthropic_key", None)
    save_settings(raw)
    return 200, public_settings()


# ---------- photo identification (Ollama local, Claude bring-your-own-key) ----------

class ProviderError(Exception):
    def __init__(self, status, detail="", timeout=False):
        super().__init__(detail)
        self.status, self.detail, self.timeout = status, detail, timeout


_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never proxy local calls


def http_json(method, url, payload=None, headers=None, timeout=60, local=False):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=dict({"Content-Type": "application/json"}, **(headers or {})))
    opener = _LOCAL if local else urllib.request.build_opener()
    try:
        with opener.open(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise ProviderError(e.code, e.read()[:2000].decode("utf-8", "replace"))
    except (socket.timeout, TimeoutError):
        raise ProviderError(None, "timed out", timeout=True)
    except (urllib.error.URLError, OSError, ValueError) as e:
        if isinstance(getattr(e, "reason", None), (socket.timeout, TimeoutError)):
            raise ProviderError(None, "timed out", timeout=True)
        raise ProviderError(None, str(e))


ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "brand": {"type": "string"},
        "item_type": {"type": "string", "enum": ITEM_TYPES + ["Other"]},
        "description": {"type": "string"},
        "search_query": {"type": "string"},
    },
    "required": ["name", "brand", "item_type", "description", "search_query"],
}

PROMPT = (
    "You are cataloging household belongings for a home inventory. Look at the photo and "
    "identify the single main item. Answer with the requested fields only.\n"
    "- name: short common name, e.g. 'Stand Mixer'.\n"
    "- brand: the manufacturer only if a logo or text shows it or you are certain; otherwise ''.\n"
    "- item_type: one of " + ", ".join(ITEM_TYPES) + ", or Other.\n"
    "- description: one factual sentence (colour, material, visible model number). "
    "Do not guess details you cannot see.\n"
    "- search_query: a web search query that would find a comparable replacement.")


def clean_str(v, limit):
    return v.strip()[:limit] if isinstance(v, str) else ""


def parse_item(content):
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except ValueError:
            m = re.search(r"\{.*\}", content, re.S)  # tolerate prose around the JSON
            try:
                content = json.loads(m.group(0)) if m else None
            except ValueError:
                content = None
    if not isinstance(content, dict) or not clean_str(content.get("name"), 120):
        raise ApiError(502, "The model did not return a usable item. Try again or use a clearer photo.")
    t = clean_str(content.get("item_type"), 40)
    t = next((x for x in ITEM_TYPES if x.lower() == t.lower()), None)
    return {"name": clean_str(content["name"], 120), "brand": clean_str(content.get("brand"), 80),
            "item_type": t, "description": clean_str(content.get("description"), 500),
            "search_query": clean_str(content.get("search_query"), 200)}


def identify_ollama(cfg, b64):
    model, base = cfg["ollama_model"], cfg["ollama_url"].rstrip("/")
    try:
        out = http_json("POST", base + "/api/chat", {
            "model": model, "stream": False, "format": ITEM_SCHEMA, "options": {"temperature": 0},
            "messages": [{"role": "user", "content": PROMPT, "images": [b64]}]},
            timeout=300, local=True)
    except ProviderError as e:
        if e.timeout:
            raise ApiError(502, "Ollama took longer than 5 minutes. Try a smaller model such as qwen2.5vl:3b.")
        if e.status is None:
            raise ApiError(502, "Could not reach Ollama at %s. Is it running? Start it with 'ollama serve'." % base)
        if e.status == 404:
            raise ApiError(502, "Model '%s' is not installed in Ollama. Run: ollama pull %s" % (model, model))
        raise ApiError(502, "Ollama returned an error (HTTP %s)." % e.status)
    return parse_item((out.get("message") or {}).get("content", ""))


def identify_claude(cfg, media, b64):
    model = cfg["anthropic_model"]
    tool = {"name": "record_item", "description": "Record the identified household item.",
            "input_schema": ITEM_SCHEMA}
    try:
        out = http_json("POST", ANTHROPIC_BASE_URL.rstrip("/") + "/v1/messages", {
            "model": model, "max_tokens": 700, "tools": [tool],
            "tool_choice": {"type": "tool", "name": "record_item"},
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media, "data": b64}},
                {"type": "text", "text": PROMPT}]}]},
            headers={"x-api-key": cfg["anthropic_key"], "anthropic-version": "2023-06-01"}, timeout=90)
    except ProviderError as e:
        if e.timeout or e.status is None:
            raise ApiError(502, "Could not reach the Anthropic API. Check your internet connection.")
        if e.status in (401, 403):
            raise ApiError(502, "Anthropic rejected the API key. Check it in Settings.")
        if e.status == 404:
            raise ApiError(502, "Anthropic does not know the model '%s'. Check the model name in Settings." % model)
        if e.status == 429:
            raise ApiError(502, "Anthropic rate limit or spending limit reached. Try again later.")
        raise ApiError(502, "Anthropic returned an error (HTTP %s)." % e.status)
    block = next((c for c in out.get("content", []) if c.get("type") == "tool_use"), None)
    return parse_item(block.get("input") if block else None)


def identify(ctx):
    b = ctx.body
    provider = b.get("provider", "ollama")
    if provider not in ("ollama", "anthropic"):
        raise ApiError(400, "provider must be ollama or anthropic")
    m = re.match(r"^data:([\w/+.-]+);base64,(.*)$", b.get("data") or "", re.S)
    if not m or m.group(1) not in IMAGE_TYPES:
        raise ApiError(400, "data must be a base64 data URL (png, jpeg, webp, gif)")
    try:
        raw = base64.b64decode(m.group(2), validate=True)
    except ValueError:
        raise ApiError(400, "invalid base64 data")
    if not raw:
        raise ApiError(400, "empty image")
    cfg = load_settings()
    if provider == "anthropic":
        if not cfg["anthropic_key"]:
            raise ApiError(400, "Claude is not set up. Add your API key in Settings.")
        if len(raw) > MAX_CLAUDE_IMAGE:
            raise ApiError(400, "Image is over Claude's 5 MB limit. Use a smaller photo.")
        result = identify_claude(cfg, m.group(1), m.group(2))
        result["model"] = cfg["anthropic_model"]
    else:
        result = identify_ollama(cfg, m.group(2))
        result["model"] = cfg["ollama_model"]
    result["provider"] = provider
    return 200, result


def providers_status(ctx):
    c = load_settings()
    st = {"ollama": {"url": c["ollama_url"], "model": c["ollama_model"], "reachable": False,
                     "model_installed": False, "models": []},
          "anthropic": {"configured": bool(c["anthropic_key"]), "model": c["anthropic_model"]}}
    try:
        tags = http_json("GET", c["ollama_url"].rstrip("/") + "/api/tags", timeout=3, local=True)
        names = [x.get("name") for x in tags.get("models", []) if isinstance(x, dict)]
        st["ollama"].update(reachable=True, models=names,
                            model_installed=c["ollama_model"] in names or c["ollama_model"] + ":latest" in names)
    except ProviderError:
        pass
    return 200, st


ID = r"(\d+)"
ROUTES = [
    ("GET", r"/meta", meta),
    ("GET", r"/homes", list_homes),
    ("POST", r"/homes", create_home),
    ("GET", r"/homes/%s" % ID, get_home),
    ("PUT", r"/homes/%s" % ID, update_home),
    ("DELETE", r"/homes/%s" % ID, delete_home),
    ("GET", r"/homes/%s/rooms" % ID, list_rooms),
    ("POST", r"/homes/%s/rooms" % ID, create_room),
    ("GET", r"/homes/%s/insurance" % ID, get_insurance),
    ("PUT", r"/homes/%s/insurance" % ID, put_insurance),
    ("DELETE", r"/homes/%s/insurance" % ID, delete_insurance),
    ("GET", r"/rooms/%s" % ID, get_room),
    ("PUT", r"/rooms/%s" % ID, update_room),
    ("DELETE", r"/rooms/%s" % ID, delete_room),
    ("GET", r"/rooms/%s/items" % ID, list_items),
    ("POST", r"/rooms/%s/items" % ID, create_item),
    ("GET", r"/items/%s" % ID, get_item),
    ("PUT", r"/items/%s" % ID, update_item),
    ("DELETE", r"/items/%s" % ID, delete_item),
    ("POST", r"/items/%s/image" % ID, upload_image),
    ("POST", r"/identify", identify),
    ("GET", r"/settings", get_settings),
    ("PUT", r"/settings", put_settings),
    ("GET", r"/providers/status", providers_status),
]
ROUTES = [(m, re.compile("^" + p + "$"), fn) for m, p, fn in ROUTES]


class Ctx:
    pass


class Handler(BaseHTTPRequestHandler):
    server_version = "HomeInventory/1.0"

    def log_message(self, fmt, *args):
        if os.environ.get("INVENTORY_QUIET") != "1":
            sys.stderr.write("%s %s\n" % (self.command, self.path))

    def send_json(self, status, obj):
        payload = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ApiError(413, "request body too large")
        if not length:
            return {}
        try:
            body = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "invalid JSON")
        if not isinstance(body, dict):
            raise ApiError(400, "JSON body must be an object")
        return body

    def handle_api(self, method, parsed):
        # Blocks cross-site writes (a page cannot send application/json without a
        # preflight, which this server never grants).
        if method in ("POST", "PUT") and int(self.headers.get("Content-Length") or 0) > 0:
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                raise ApiError(415, "Content-Type must be application/json")
        path = parsed.path[4:] or "/"
        if len(path) > 1:
            path = path.rstrip("/")
        matched = False
        for m, rx, fn in ROUTES:
            mt = rx.match(path)
            if not mt:
                continue
            matched = True
            if m != method:
                continue
            ctx = Ctx()
            ctx.query = parse_qs(parsed.query)
            ctx.body = self.read_body() if method in ("POST", "PUT") else {}
            ctx.db = connect()
            try:
                status, obj = fn(ctx, *[int(g) for g in mt.groups()]) \
                    if mt.groups() else fn(ctx)
            finally:
                ctx.db.close()
            return self.send_json(status, obj)
        raise ApiError(405 if matched else 404, "method not allowed" if matched else "not found")

    def serve_file(self, base, rel, types):
        full = os.path.realpath(os.path.join(base, rel))
        if not full.startswith(os.path.realpath(base) + os.sep) or not os.path.isfile(full):
            raise ApiError(404, "not found")
        ext = full.rsplit(".", 1)[-1].lower()
        ctype = types.get(ext, "application/octet-stream")
        with open(full, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def dispatch(self, method):
        parsed = urlparse(self.path)
        try:
            extra = [h.strip().lower() for h in os.environ.get("INVENTORY_ALLOWED_HOSTS", "").split(",") if h.strip()]
            host = self.headers.get("Host") or ""
            if not ALLOWED_HOSTS.match(host) and host.rsplit(":", 1)[0].lower() not in extra:
                raise ApiError(403, "Forbidden host")  # defeats DNS-rebinding attacks
            if parsed.path == "/api" or parsed.path.startswith("/api/"):
                return self.handle_api(method, parsed)
            if method != "GET":
                raise ApiError(405, "method not allowed")
            if parsed.path.startswith("/images/"):
                return self.serve_file(images_dir(), parsed.path[8:], {
                    "png": "image/png", "jpg": "image/jpeg", "webp": "image/webp",
                    "gif": "image/gif"})
            rel = "index.html" if parsed.path == "/" else parsed.path.lstrip("/")
            return self.serve_file(STATIC_DIR, rel, {
                "html": "text/html; charset=utf-8", "js": "text/javascript",
                "css": "text/css", "json": "application/json", "svg": "image/svg+xml"})
        except ApiError as e:
            self.send_json(e.status, {"error": e.message})
        except Exception as e:  # keep the server alive on bugs
            sys.stderr.write("error: %r\n" % (e,))
            self.send_json(500, {"error": "internal server error"})

    def do_GET(self): self.dispatch("GET")
    def do_POST(self): self.dispatch("POST")
    def do_PUT(self): self.dispatch("PUT")
    def do_DELETE(self): self.dispatch("DELETE")


def make_server(host="127.0.0.1", port=8000, data_dir=None):
    global DATA_DIR
    if data_dir:
        DATA_DIR = data_dir
    init_storage()
    return ThreadingHTTPServer((host, port), Handler)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    srv = make_server(port=port)  # bound to localhost only; add auth before changing
    print("Home Inventory running at http://localhost:%d  (data: %s)" % (port, DATA_DIR))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
