"""Home inventory report: gathers every item of a home and lays it out as a PDF (pdfwriter.py)."""
import datetime
import os

import pdfwriter as pw

INK, MUTED, LINE = (0.13, 0.12, 0.10), (0.42, 0.40, 0.36), (0.80, 0.78, 0.72)
BAND, ACCENT, WARN = (0.93, 0.91, 0.86), (0.64, 0.28, 0.17), (0.72, 0.47, 0.12)


def money(v):
    try:
        v = float(v or 0)
    except (TypeError, ValueError):
        return "-"
    return "-" if not v else "${:,.2f}".format(v)


def nice_date(s):
    try:
        return datetime.datetime.strptime((s or "")[:10], "%Y-%m-%d").strftime("%b %d, %Y").replace(" 0", " ")
    except ValueError:
        return ""


def gather(conn, home_id, home):
    """The home with its rooms and every item, plus totals."""
    rooms = []
    for r in conn.execute("SELECT * FROM rooms WHERE home_id = ? ORDER BY name COLLATE NOCASE", (home_id,)):
        r = dict(r)
        r["items"] = [dict(i) for i in conn.execute(
            "SELECT * FROM items WHERE room_id = ? ORDER BY name COLLATE NOCASE, id", (r["id"],))]
        for i in r["items"]:
            i["replacement_date"] = (i.get("price_updated_at") or "")[:10]
        r["total_purchased"] = sum(i["purchased_value"] or 0 for i in r["items"])
        r["total_replacement"] = sum(i["replacement_value"] or 0 for i in r["items"])
        rooms.append(r)
    return {"home": home, "rooms": rooms,
            "item_count": sum(len(r["items"]) for r in rooms),
            "total_purchased": sum(r["total_purchased"] for r in rooms),
            "total_replacement": sum(r["total_replacement"] for r in rooms)}


def _kv_line(d, label, value, x, w, size=9):
    if value:
        d.paragraph("%s: %s" % (label, value), size=size, x=x, width=w, color=INK)


def _summary(d, data, today, show_price=True):
    h = data["home"]
    d.y -= 4
    d.text(d.left, d.y - 18, "Home inventory report", "F2", 20, ACCENT)
    d.y -= 26
    d.paragraph(h["name"], "F2", 14, INK, gap=1)
    if h.get("address"):
        d.paragraph(h["address"], size=10, color=MUTED)
    d.paragraph("Prepared " + today.strftime("%b %d, %Y").replace(" 0", " "), size=9, color=MUTED, gap=6)
    boxes = [("Rooms", str(len(data["rooms"]))), ("Items", str(data["item_count"]))]
    if show_price:
        boxes.append(("Purchased", money(data["total_purchased"])))
    boxes.append(("Replacement value", money(data["total_replacement"])))
    bw = d.width / len(boxes)
    d.ensure(44)
    d.rect(d.left, d.y - 38, d.width, 38, BAND)
    for n, (k, v) in enumerate(boxes):
        x = d.left + n * bw + 10
        d.text(x, d.y - 14, k, size=8, color=MUTED)
        d.text(x, d.y - 29, v, "F2", 13, INK)
    d.y -= 48
    ins = h.get("insurance") or {}
    if ins.get("configured"):
        d.paragraph("Insurance", "F2", 11, INK, gap=2)
        bits = [("Insurer", ins.get("insurer")), ("Policy #", ins.get("policy_number")), ("Expires", nice_date(ins.get("renewal_date"))),
                ("Website", ins.get("website")), ("Contact", ins.get("contact"))]
        d.paragraph("  |  ".join("%s: %s" % (k, v) for k, v in bits if v) or "On file", size=9, color=INK)
        limits = [("Dwelling", ins.get("dwelling_coverage")), ("Contents", ins.get("contents_coverage")),
                  ("Liability", ins.get("liability_coverage")), ("Deductible", ins.get("deductible"))]
        d.paragraph("  |  ".join("%s: %s" % (k, money(v)) for k, v in limits if v), size=9, color=INK)
        for r in ins.get("riders") or []:
            d.paragraph("Rider: %s - %s" % (r.get("name", ""), money(r.get("limit"))), size=9, color=INK)
        d.y -= 6


def _room_header(d, room):
    d.ensure(60)
    d.rect(d.left, d.y - 22, d.width, 22, BAND)
    d.text(d.left + 8, d.y - 15, room["name"], "F2", 12, INK)
    d.text_right(d.right - 8, d.y - 15, "%d item%s   Replacement %s" % (
        len(room["items"]), "" if len(room["items"]) == 1 else "s", money(room["total_replacement"])), size=9, color=MUTED)
    d.y -= 28


def _table(d, room, cols, show_price=True):
    x0 = d.left
    xs = [x0]
    for w in cols:
        xs.append(xs[-1] + w)

    def head():
        d.ensure(40)
        for label, x, w in zip(("Item", "Details", "Purchased", "Replacement"), xs, cols):
            if label in ("Purchased", "Replacement"):
                d.text(x + 2, d.y - 9, label, "F2", 8, MUTED)
            else:
                d.text(x + 2, d.y - 9, label, "F2", 8, MUTED)
        d.y -= 13
        d.line(d.left, d.y, d.right, d.y)

    head()
    for it in room["items"]:
        c1 = [(it["name"], "F2", 9, INK), ("%s%s" % (it["inv_no"] or "", " · " + it["item_type"] if it["item_type"] else ""), "F1", 8, MUTED)]
        c2 = []
        bm = " ".join(x for x in (it["brand"], it["model_number"]) if x)
        if bm:
            c2.append((bm, "F1", 9, INK))
        if it["description"]:
            c2.append((it["description"], "F1", 8, MUTED))
        c3 = [(money(it["purchased_value"]), "F1", 9, INK)] if show_price else []
        if it["purchased_date"]:
            c3.append((nice_date(it["purchased_date"]), "F1", 8, MUTED))
        if it["purchased_condition"]:
            c3.append((it["purchased_condition"], "F1", 8, MUTED))
        c4 = [(money(it["replacement_value"]), "F1", 9, INK)]
        if it["replacement_date"]:
            c4.append((nice_date(it["replacement_date"]), "F1", 8, MUTED))
        cells = [c1, c2, c3, c4]
        laid = [[(ln, f, s, col) for (t, f, s, col) in c for ln in pw.wrap(t, w - 6, f, s)] for c, w in zip(cells, cols)]
        h = max(len(c) and sum(s * 1.25 for (_, _, s, _) in c) for c in laid) + 6
        if d.ensure(h + 4):
            head()
        y = d.y - 3
        for c, x in zip(laid, xs):
            yy = y
            for (ln, f, s, col) in c:
                yy -= s * 1.25
                d.text(x + 2, yy + s * 0.25, ln, f, s, col)
        d.y -= h
        d.line(d.left, d.y, d.right, d.y, color=(0.9, 0.88, 0.84), width=0.4)


def _cards(d, room, images_dir, show_price=True):
    iw, ih = 96, 74
    tw = d.width - iw - 14
    for it in room["items"]:
        lines = [(it["name"], "F2", 10.5, INK)]
        sub = "  ·  ".join(x for x in (it["inv_no"], it["item_type"], " ".join(y for y in (it["brand"], it["model_number"]) if y)) if x)
        if sub:
            lines.append((sub, "F1", 8.5, MUTED))
        if it["description"]:
            lines.append((it["description"], "F1", 9, INK))
        extra = [x for x in (nice_date(it["purchased_date"]), it["purchased_condition"]) if x]
        paid = "  ·  ".join(([money(it["purchased_value"])] if show_price else []) + extra)
        if paid:
            lines.append(("Purchased: " + paid, "F1", 9, INK))
        rep = "Replacement: %s" % money(it["replacement_value"])
        if it["replacement_date"]:
            rep += "  (as of %s)" % nice_date(it["replacement_date"])
        lines.append((rep, "F1", 9, INK))
        if it["similar_url"]:
            lines.append(("Similar item: " + it["similar_url"], "F1", 7.5, MUTED))
        laid = [(ln, f, s, col) for (t, f, s, col) in lines for ln in pw.wrap(t, tw, f, s)]
        h = max(ih, sum(s * 1.3 for (_, _, s, _) in laid)) + 10
        d.ensure(h)
        top = d.y - 4
        img = None
        if it.get("image"):
            try:
                with open(os.path.join(images_dir, it["image"]), "rb") as fh:
                    img = pw.load_image(fh.read())
            except OSError:
                img = None
        if img:
            k = min(iw / img.width, ih / img.height)
            w, hh = img.width * k, img.height * k
            d.image(img, d.left + (iw - w) / 2, top - hh, w, hh)
        else:
            d.rect(d.left, top - ih, iw, ih, (0.95, 0.94, 0.91))
            d.text(d.left + 24, top - ih / 2 - 3, "No photo" if not it.get("image") else "Photo not shown", size=7.5, color=MUTED)
        y = top
        for (ln, f, s, col) in laid:
            y -= s * 1.3
            d.text(d.left + iw + 14, y + s * 0.25, ln, f, s, col)
        d.y -= h
        d.line(d.left, d.y + 3, d.right, d.y + 3, color=(0.9, 0.88, 0.84), width=0.4)


def build_pdf(data, with_photos, images_dir, today=None, show_price=True):
    today = today or datetime.date.today()
    d = pw.Document("Home inventory report - " + data["home"]["name"], footer=data["home"]["name"] + " inventory")
    _summary(d, data, today, show_price)
    cols = (150, 190, 82, d.width - 150 - 190 - 82)
    for room in data["rooms"]:
        _room_header(d, room)
        if not room["items"]:
            d.paragraph("No items in this room.", size=9, color=MUTED, gap=8)
            continue
        (_cards(d, room, images_dir, show_price) if with_photos else _table(d, room, cols, show_price))
        d.y -= 10
    if not data["rooms"]:
        d.paragraph("This home has no rooms yet.", size=10, color=MUTED)
    return d.render()
