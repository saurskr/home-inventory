"""Read a product page and pull out its price. Standard library only.

    from linkinfo import link_info
    link_info("https://shop.example/item")  # -> {"url", "host", "title", "price", "currency", "source"}

The server fetches a URL it was handed, so this module is deliberately defensive:
 - only http/https, and no redirects to anything else (no ftp:, file:)
 - every address the host resolves to must be a public one (no localhost, home network or
   cloud-metadata addresses), checked again for each redirect, and the connection goes to
   the address that was checked (no DNS-rebinding window)
 - at most 3 redirects, 10 s timeout, 1.5 MB read, HTML only
Prices come from structured data the site publishes: JSON-LD, Open Graph / product meta tags,
itemprop="price", and the page's embedded product data (for example Next.js __NEXT_DATA__), where the
price is only taken from the product whose id appears in the link. It never guesses from page text,
so it may find nothing. Sites that show a "robot check" instead of the page are detected and reported
as such. Diagnose a link with: python3 tools/check_link.py <url>
"""
import html.parser
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request

MAX_BYTES = 3_000_000
TIMEOUT = 10
# A plain desktop-browser identity: many shops refuse requests that announce themselves as scripts.
# Override with the INVENTORY_FETCH_UA environment variable.
USER_AGENT = os.environ.get("INVENTORY_FETCH_UA") or (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36")
BLOCKED_STATUS = (401, 403, 412, 418, 429, 503)
ROBOT_MARKERS = ("px-captcha", "robot or human", "press & hold", "press &amp; hold", "are you a human",
                 "pardon our interruption", "captcha-delivery.com", "incapsula incident", "access denied",
                 "unusual traffic from your computer", "verify you are human", "/_incapsula_resource")


class LinkError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.message, self.status = message, status


def _check_public(host, allow_private):
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise LinkError("That address could not be found. Check the link.", 400)
    ips = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        if not allow_private and not ip.is_global:
            raise LinkError("That link points to a private or local address, so it was not opened.", 400)
        ips.append(ip)
    if not ips:
        raise LinkError("That address could not be found. Check the link.", 400)
    return str(sorted(ips, key=lambda i: i.version)[0])


def _make_opener(allow_private):
    ctx = ssl.create_default_context()

    class HTTPConn(http.client.HTTPConnection):
        def connect(self):
            ip = _check_public(self.host, allow_private)
            self.sock = socket.create_connection((ip, self.port), self.timeout)

    class HTTPSConn(http.client.HTTPSConnection):
        def connect(self):
            ip = _check_public(self.host, allow_private)
            sock = socket.create_connection((ip, self.port), self.timeout)
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)

    class HTTP(urllib.request.AbstractHTTPHandler):
        def http_open(self, req):
            return self.do_open(HTTPConn, req)
        http_request = urllib.request.AbstractHTTPHandler.do_request_

    class HTTPS(urllib.request.AbstractHTTPHandler):
        def https_open(self, req):
            return self.do_open(HTTPSConn, req, context=ctx)
        https_request = urllib.request.AbstractHTTPHandler.do_request_

    class Redirect(urllib.request.HTTPRedirectHandler):
        max_redirections = 3

        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if not re.match(r"^https?://", newurl, re.I):
                raise LinkError("The link redirects somewhere that is not a web page.", 400)
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    op = urllib.request.OpenerDirector()   # only these handlers: no ftp/file/proxy
    for h in (urllib.request.UnknownHandler(), urllib.request.HTTPDefaultErrorHandler(), Redirect(),
              urllib.request.HTTPErrorProcessor(), HTTP(), HTTPS()):
        op.add_handler(h)
    return op


def fetch_raw(url, allow_private=False):
    """(status, final_url, content_type, body_text). HTTP error pages are returned, not raised."""
    if not re.match(r"^https?://[^\s/]+", url or "", re.I):
        raise LinkError("Paste a full link starting with http:// or https://", 400)
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "identity", "Upgrade-Insecure-Requests": "1"})
    try:
        try:
            r = _make_opener(allow_private).open(req, timeout=TIMEOUT)
            status = r.status
        except urllib.error.HTTPError as e:      # the body of an error page is still worth reading
            r, status = e, e.code
        with r:
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            body = r.read(MAX_BYTES)
            charset = r.headers.get_content_charset() or "utf-8"
            final = r.geturl() if hasattr(r, "geturl") else url
    except LinkError:
        raise
    except (socket.timeout, TimeoutError):
        raise LinkError("The site took too long to answer.")
    except (urllib.error.URLError, OSError, ValueError) as e:
        if isinstance(getattr(e, "reason", None), LinkError):
            raise e.reason
        raise LinkError("Could not open that link.")
    try:
        text = body.decode(charset, "replace")
    except LookupError:
        text = body.decode("utf-8", "replace")
    return status, final, ctype, text


def looks_like_robot_check(doc):
    low = doc[:200_000].lower()
    return any(m in low for m in ROBOT_MARKERS)


BLOCKED_MESSAGE = ("That site showed a robot check instead of the product page, so the price could not be read. "
                   "Enter the price by hand.")


def fetch_page(url, allow_private=False):
    """(final_url, html) for a normal page; raises LinkError with a plain-language reason otherwise."""
    status, final, ctype, doc = fetch_raw(url, allow_private)
    if status in (404, 410):
        raise LinkError("That page was not found. Check the link.", 400)
    if status in BLOCKED_STATUS:
        raise LinkError(BLOCKED_MESSAGE if looks_like_robot_check(doc) else
                        "That site did not allow the page to be read automatically. Enter the price by hand.")
    if status >= 400:
        raise LinkError("The site returned an error (HTTP %s)." % status)
    if ctype not in ("text/html", "application/xhtml+xml", ""):
        raise LinkError("That link is not a web page.", 400)
    return final, doc


# ---------- price extraction ----------

def parse_price(v):
    """12.5, '$1,299.00', '12,99' -> float, or None (zero and negatives are rejected)."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        f = float(v)
    else:
        s = re.sub(r"[^\d.,]", "", str(v))
        if not re.search(r"\d", s):
            return None
        if "," in s and "." in s:
            s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
        elif "," in s:
            s = s.replace(",", ".") if re.search(r",\d{1,2}$", s) else s.replace(",", "")
        try:
            f = float(s)
        except ValueError:
            return None
    return round(f, 2) if 0 < f < 10_000_000 else None


class _Page(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ld, self.blobs, self.meta, self.itemprop, self.title = [], [], {}, [], ""
        self._script = self._title = False
        self._kind = "ld"
        self._buf = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        t = a.get("type", "").lower()
        if tag == "script" and "ld+json" in t:
            self._script, self._kind, self._buf = True, "ld", []
        elif tag == "script" and (a.get("id") == "__NEXT_DATA__" or t in ("application/json", "text/json")):
            self._script, self._kind, self._buf = True, "json", []
        elif tag == "title":
            self._title, self._buf = True, []
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if key and "content" in a:
                self.meta.setdefault(key, a["content"])
        if a.get("itemprop", "").lower() == "price" and a.get("content"):
            self.itemprop.append(a["content"])
        if a.get("itemprop", "").lower() == "pricecurrency" and a.get("content"):
            self.meta.setdefault("pricecurrency", a["content"])

    def handle_data(self, data):
        if self._script or self._title:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._script:
            (self.ld if self._kind == "ld" else self.blobs).append("".join(self._buf))
            self._script = False
        elif tag == "title" and self._title:
            self.title = " ".join("".join(self._buf).split())
            self._title = False


def _walk(node, out):
    if isinstance(node, list):
        for n in node:
            _walk(n, out)
    elif isinstance(node, dict):
        t = node.get("@type")
        types = t if isinstance(t, list) else [t]
        if "Product" in types or "ProductGroup" in types:
            out.append(node)
        for v in node.values():
            if isinstance(v, (list, dict)):
                _walk(v, out)


def _offer_price(offers):
    for o in (offers if isinstance(offers, list) else [offers]):
        if not isinstance(o, dict):
            continue
        spec = o.get("priceSpecification")
        spec = spec[0] if isinstance(spec, list) and spec else spec
        for cand, cur in ((o.get("price"), o.get("priceCurrency")), (o.get("lowPrice"), o.get("priceCurrency")),
                          ((spec or {}).get("price") if isinstance(spec, dict) else None,
                           (spec or {}).get("priceCurrency") if isinstance(spec, dict) else None)):
            p = parse_price(cand)
            if p:
                return p, (cur or "").upper()
        nested = o.get("offers")
        if nested:
            r = _offer_price(nested)
            if r:
                return r
    return None


ID_KEYS = ("usItemId", "itemId", "productId", "id", "sku", "us_item_id", "legacyItemId", "asin")


def item_id_from_url(url):
    """A product id found in the link (Walmart /ip/<name>/<number>, Amazon /dp/<ASIN>), lower-cased."""
    path = urllib.parse.urlparse(url or "").path
    m = re.search(r"/(?:dp|gp/product)/([A-Za-z0-9]{10})(?:[/?]|$)", path) or re.search(r"/(\d{5,})(?:[/?]|$)", path + "/")
    return m.group(1).lower() if m else None


def _money_in(v):
    """(price, currency) from a number, a string like '$99.00', or {price|value|amount|priceString: ...}."""
    if isinstance(v, dict):
        cur = str(v.get("currencyUnit") or v.get("currency") or v.get("priceCurrency") or "").upper()
        for k in ("price", "value", "amount", "priceValue", "priceString", "displayPrice"):
            p = parse_price(v.get(k))
            if p:
                return p, cur
        return None
    p = parse_price(v)
    return (p, "") if p else None


def _price_of_product(node):
    info = node.get("priceInfo")
    cands = [info.get("currentPrice") if isinstance(info, dict) else None, node.get("currentPrice"),
             node.get("offerPrice"), node.get("salePrice"), node.get("price")]
    for c in cands:
        r = _money_in(c)
        if r:
            return r
    return None


def _price_from_page_data(blobs, item_id):
    """Price of the product whose id matches the link, found in the page's embedded JSON. Pages also carry
    other products' prices (carousels, ads), so without an id match nothing is taken."""
    if not item_id:
        return None
    stack = []
    for text in blobs:
        try:
            stack.append(json.loads(text))
        except ValueError:
            continue
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if any(str(node.get(k, "")).lower() == item_id for k in ID_KEYS):
                r = _price_of_product(node)
                if r:
                    return r
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            stack.extend(v for v in node if isinstance(v, (dict, list)))
    return None


def extract_price(doc, url=""):
    """Returns {"price": float|None, "currency": str, "title": str, "source": str}."""
    page = _Page()
    try:
        page.feed(doc)
    except Exception:  # malformed HTML: use what was gathered
        pass
    title = page.meta.get("og:title") or page.title
    for blob in page.ld:
        try:
            data = json.loads(blob)
        except ValueError:
            continue
        found = []
        _walk(data, found)
        for prod in found:
            r = _offer_price(prod.get("offers"))
            if r:
                return {"price": r[0], "currency": r[1], "title": str(prod.get("name") or title)[:200],
                        "source": "json-ld"}
    found = _price_from_page_data(page.blobs, item_id_from_url(url))
    if found:
        return {"price": found[0], "currency": found[1], "title": title[:200], "source": "page-data"}
    cur = (page.meta.get("product:price:currency") or page.meta.get("og:price:currency")
           or page.meta.get("pricecurrency") or "").upper()
    for key in ("product:price:amount", "og:price:amount", "og:price", "twitter:data1"):
        p = parse_price(page.meta.get(key))
        if p:
            if key == "twitter:data1" and not re.search(r"[$€£]|\d[.,]\d\d", page.meta.get(key, "")):
                continue
            return {"price": p, "currency": cur, "title": title[:200], "source": "meta"}
    for content in page.itemprop:
        p = parse_price(content)
        if p:
            return {"price": p, "currency": cur, "title": title[:200], "source": "itemprop"}
    return {"price": None, "currency": cur, "title": title[:200], "source": ""}


def _image_urls(value):
    """URLs inside a JSON-LD "image" value: a string, a list, or {"url": ...} objects."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        yield from _image_urls(value.get("url") or value.get("contentUrl"))
    elif isinstance(value, list):
        for v in value:
            yield from _image_urls(v)


def extract_image(doc, base=""):
    """The product picture a page names: Product image in JSON-LD, then og:image / twitter:image. Absolute URL or ''."""
    page = _Page()
    try:
        page.feed(doc)
    except Exception:
        pass
    cands = []
    for blob in page.ld:
        try:
            data = json.loads(blob)
        except ValueError:
            continue
        found = []
        _walk(data, found)
        for prod in found:
            cands.extend(_image_urls(prod.get("image")))
    cands += [page.meta.get(k, "") for k in ("og:image", "og:image:secure_url", "twitter:image", "twitter:image:src", "image")]
    for c in cands:
        c = (c or "").strip()
        if not c or c.startswith("data:"):
            continue
        u = urllib.parse.urljoin(base, c)
        if re.match(r"^https?://", u, re.I):
            return u
    return ""


IMAGE_MAX_BYTES = 4_000_000


def _sniff_image(b):
    if b[:3] == b"\xff\xd8\xff":
        return "image/jpeg", "jpg"
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", "png"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp", "webp"
    if b[:4] == b"GIF8":
        return "image/gif", "gif"
    return None


def fetch_image(url, allow_private=False):
    """(bytes, mime, ext) of a picture at a public http(s) address, same safety checks as pages."""
    if not re.match(r"^https?://[^\s/]+", url or "", re.I):
        raise LinkError("The page's picture link is not a web address.", 400)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "image/*,*/*;q=0.5",
                                               "Accept-Encoding": "identity"})
    try:
        with _make_opener(allow_private).open(req, timeout=TIMEOUT) as r:
            if r.status >= 400:
                raise LinkError("The picture could not be downloaded.", 400)
            body = r.read(IMAGE_MAX_BYTES + 1)
    except LinkError:
        raise
    except (socket.timeout, TimeoutError):
        raise LinkError("The picture took too long to download.")
    except urllib.error.HTTPError:
        raise LinkError("The site would not give the picture.", 400)
    except (urllib.error.URLError, OSError, ValueError) as e:
        if isinstance(getattr(e, "reason", None), LinkError):
            raise e.reason
        raise LinkError("The picture could not be downloaded.")
    if len(body) > IMAGE_MAX_BYTES:
        raise LinkError("The page's picture is too large (over 4 MB).", 400)
    kind = _sniff_image(body)
    if not kind:
        raise LinkError("The page's picture is not a PNG, JPEG, WebP or GIF image.", 400)
    return body, kind[0], kind[1]


def link_info(url, allow_private=False):
    final, doc = fetch_page(url, allow_private)
    out = extract_price(doc, final)
    out["image"] = extract_image(doc, final)
    if out["price"] is None and looks_like_robot_check(doc):
        raise LinkError(BLOCKED_MESSAGE)
    out.update(url=final, host=re.sub(r"^www\.", "", urllib.parse.urlparse(final).hostname or ""))
    return out
