#!/usr/bin/env python3
"""Home Inventory: local-first API + static UI. Standard library only."""
import base64
import json
import os
import re
import sqlite3
import sys
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
  source TEXT NOT NULL DEFAULT 'manual'
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
    conn.commit()
    conn.close()


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
    purchased = purchased if purchased is not None else 0
    replacement = replacement if replacement is not None else purchased
    src = b.get("source", source)
    if src not in SOURCES:
        raise ApiError(400, "source must be one of: " + ", ".join(SOURCES))
    cur = conn.execute(
        """INSERT INTO items (room_id, name, description, brand, purchased_value,
           replacement_value, similar_url, item_type, source)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (room_id, text(b, "name", True), text(b, "description"), text(b, "brand"),
         purchased, replacement, text(b, "similar_url"),
         check_type(b.get("item_type")), src))
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
    for k in ("description", "brand", "similar_url"):
        if k in b:
            f[k] = text(b, k)
    if "item_type" in b:
        f["item_type"] = check_type(b["item_type"])
    if "purchased_value" in b:
        v = money(b, "purchased_value")
        f["purchased_value"] = v if v is not None else 0
    if "replacement_value" in b:
        v = money(b, "replacement_value")
        f["replacement_value"] = v if v is not None else f["purchased_value"]
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
           replacement_value=?, similar_url=?, item_type=?, source=?, room_id=?
           WHERE id=?""",
        (f["name"], f["description"], f["brand"], f["purchased_value"],
         f["replacement_value"], f["similar_url"], f["item_type"], f["source"],
         f["room_id"], id_))
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


def identify(ctx):
    # Reserved for photo-to-item via a local vision model (Ollama).
    return 501, {"error": "photo identification is not implemented yet"}


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
