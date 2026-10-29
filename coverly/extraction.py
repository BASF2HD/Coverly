from __future__ import annotations

import json
import re
from html import unescape
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode, unquote
from bs4 import BeautifulSoup
from .network import canonical_url
from .metadata import source_metadata


def image_key(url):
    p = urlsplit(url)
    path = re.sub(r"-\d{2,5}x\d{2,5}(?=\.[a-zA-Z]+$)", "", p.path)
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.lower() not in {"w", "h", "width", "height", "resize", "quality"}])
    return urlunsplit((p.scheme, p.netloc, path, query, ""))


def structured_products(soup):
    products = []
    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            types = value.get("@type", [])
            if isinstance(types, str):
                types = [types]
            if "Product" in types:
                products.append(value)
            for key in ("@graph", "mainEntity"):
                if key in value:
                    visit(value[key])
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            visit(json.loads(script.get_text()))
        except (ValueError, TypeError, RecursionError):
            continue
    return products


def catalogue_products(soup, url):
    """Read listing cards only; gallery images are inspected after selection."""
    root = soup.select_one('main, #main') or soup
    products = {}
    for card in root.select('.products > .product, .product-list > .product, .products-grid > .product'):
        if card.find_parent(class_=re.compile('related|upsell|recommend', re.I)):
            continue
        if 'product-category' in card.get('class', []):
            continue
        heading = card.select_one('.woocommerce-loop-product__title, .wd-entities-title, .name, h2, h3')
        anchor = (heading.select_one('a[href]') or heading.find_parent('a', href=True)) if heading else None
        anchor = anchor or card.select_one('a.woocommerce-LoopProduct-link[href], a.woocommerce-loop-product__link[href], a.product-image-link[href], .box-image a[href], a[href]')
        if anchor is None:
            continue
        if any(x in anchor.get('href', '').lower() for x in ('add-to-cart', 'quick-view', 'wishlist')):
            continue
        try:
            destination = canonical_url(urljoin(url, anchor['href']))
        except ValueError:
            continue
        if urlsplit(destination).hostname != urlsplit(url).hostname:
            continue
        image = card.select_one('img')
        title = (heading.get_text(' ', strip=True) if heading else anchor.get_text(' ', strip=True)) or anchor.get('aria-label', '') or (image.get('alt', '') if image else '')
        if not title:
            continue
        products[destination] = {'url': destination, 'metadata': {'title': title[:1000], 'structured': False},
                                 'images': [], 'pending_gallery': True, 'is_product': True, 'links': [],
                                 'adapter': 'woocommerce-catalogue'}
    return list(products.values())


PAGE_QUERY = {'page', 'paged', 'product-page'}


def page_number(url):
    parsed = urlsplit(url)
    match = re.search(r'/page/(\d+)/?$', parsed.path)
    values = dict(parse_qsl(parsed.query))
    return int(match[1]) if match else next((int(values[k]) for k in PAGE_QUERY if values.get(k, '').isdigit()), 1)


def catalogue_base(url):
    parsed = urlsplit(canonical_url(url))
    return parsed.hostname, unquote(re.sub(r'/page/\d+/?$', '/', parsed.path)).rstrip('/'), tuple(
        sorted((k,v) for k,v in parse_qsl(parsed.query, keep_blank_values=True) if k not in PAGE_QUERY))


def scoped_page_url(candidate, root):
    """Accept only numbered pages of the exact selected catalogue and filters."""
    try:
        candidate, root = canonical_url(candidate), canonical_url(root)
    except ValueError:
        return None
    target, seed = urlsplit(candidate), urlsplit(root)
    if catalogue_base(candidate)[:2] != catalogue_base(root)[:2]:
        return None
    filters = dict(catalogue_base(root)[2])
    query = dict(parse_qsl(target.query, keep_blank_values=True))
    if any(k not in PAGE_QUERY and (k not in filters or filters[k] != v) for k,v in query.items()):
        return None
    path = re.sub(r'/page/1/?$', '/', target.path)
    return canonical_url(urlunsplit((seed.scheme, target.netloc, path, urlencode({**filters, **query}), '')))


def catalogue_pagination(soup, url):
    links = {}
    for a in soup.select('.woocommerce-pagination a[href], .wd-pagination a[href], .page-numbers a[href], a.page-numbers[href], a.page-number[href], a[rel~="next"][href]'):
        target = scoped_page_url(urljoin(url, a['href']), url)
        if target and target != canonical_url(url):
            links[target] = {'url':target, 'pagination':True, 'product':False, 'number':page_number(target)}
    return sorted(links.values(), key=lambda link:link['number'])


class GenericAdapter:
    name = "generic"

    def extract(self, html, url):
        soup = BeautifulSoup(html, "html.parser")
        products = structured_products(soup)
        h1 = soup.select_one("h1.product_title, h1.entry-title, h1")
        product = products[0] if products else {}
        listing = bool(soup.select_one(".products, .product-list, .products-grid")) and not soup.select_one("h1.product_title, .woocommerce-product-gallery, .product-detail")
        is_product = not listing and bool(product or soup.select_one(".woocommerce-product-gallery, .product-detail, [itemtype$='/Product']"))
        product_heading = soup.select_one('h1.product_title')
        title = unescape(str((product_heading.get_text(' ', strip=True) if product_heading else '') or product.get("name") or (h1.get_text(" ", strip=True) if h1 else "")))[:1000]
        meta = {"title": title, "structured": bool(product), "description": str(product.get("description", ""))[:10000]}
        table = {}
        for row in soup.select("tr"):
            cells = row.select("th, td")
            if len(cells) >= 2:
                table[cells[0].get_text(" ", strip=True).lower().rstrip(":")] = cells[1].get_text(" ", strip=True)
        for aliases, field in [(("music", "composer", "artist", "music director", "music direction", "artists"), "artist"), (("album releasing year", "release year", "year"), "year"),
                               (("label", "record label"), "label"), (("serial no.", "catalogue number", "catalogue", "sku"), "catalogue")]:
            for alias in aliases:
                if alias in table:
                    meta[field] = table[alias]
                    break
        meta["captions"] = []
        if not meta.get("catalogue") and product.get("sku"):
            meta["catalogue"] = str(product["sku"])
        brand = product.get("brand", {})
        if not meta.get("label") and isinstance(brand, dict):
            meta["label"] = str(brand.get("name", ""))
        groups = {}

        def add(raw, caption="", priority=0, width=0):
            if not raw or not isinstance(raw, str) or raw.startswith(("data:", "blob:", "javascript:")):
                return
            try:
                absolute = canonical_url(urljoin(url, raw))
            except ValueError:
                return
            key = image_key(absolute)
            group = groups.setdefault(key, {"caption": caption[:500], "alternatives": []})
            if not any(x["url"] == absolute for x in group["alternatives"]):
                group["alternatives"].append({"url": absolute, "priority": priority, "width": width})
            if caption and not group.get("caption"):
                group["caption"] = caption[:500]

        # Restrict to the actual gallery, excluding recommendations, logos and menu art.
        gallery = soup.select_one(".woocommerce-product-gallery, .product-gallery, .product-detail, [itemtype$='/Product']")
        scope = gallery
        if is_product:
            for im in (scope.select("img") if scope else soup.select('img[itemprop="image"]')):
                if im.find_parent(class_=re.compile("related|upsell|recommend", re.I)):
                    continue
                caption = im.get("alt", "")
                meta["captions"].append(caption)
                attrs = []
                parent = im.find_parent("a")
                if parent and re.search(r"\.(?:jpe?g|png|webp|tiff?)(?:\?|$)", parent.get("href", ""), re.I):
                    attrs.append((parent["href"], 100, 0))
                for attr in ("data-large_image", "data-large-file", "data-full", "data-zoom-image", "data-original"):
                    raw_width = re.search(r"\d+", str(im.get("data-large_image_width", "0")))
                    attrs.append((im.get(attr), 100, int(raw_width[0]) if raw_width else 0))
                for attr in ("srcset", "data-srcset"):
                    for entry in str(im.get(attr, "")).split(","):
                        bits = entry.strip().split()
                        if bits:
                            match = re.match(r"(\d+)(w|x)", bits[-1]) if len(bits) > 1 else None
                            width = int(match[1]) * (1000 if match[2] == "x" else 1) if match else 0
                            attrs.append((bits[0], 60, width))
                attrs += [(im.get("data-src"), 30, 0), (im.get("data-lazy-src"), 30, 0), (im.get("src"), 10, 0)]
                # All alternatives in one element refer to one image even when CDN paths differ.
                valid = [(r, p, w) for r, p, w in attrs if r and not str(r).startswith("data:")]
                if valid:
                    first = image_key(urljoin(url, valid[0][0]))
                    for raw, priority, width in valid:
                        add(raw, caption, priority, width)
                        key = image_key(urljoin(url, raw))
                        if key != first and key in groups:
                            moved = groups.pop(key)
                            groups.setdefault(first, {"caption": caption, "alternatives": []})["alternatives"].extend(moved["alternatives"])
            raw_images = product.get("image", [])
            if isinstance(raw_images, (str, dict)):
                raw_images = [raw_images]
            for item in raw_images:
                add(item.get("url") or item.get("contentUrl") if isinstance(item, dict) else item, title, 90)
        images = []
        for group in groups.values():
            unique = {x["url"]: x for x in group["alternatives"]}
            group["alternatives"] = sorted(unique.values(), key=lambda x: (x["priority"], x["width"]), reverse=True)
            group["url"] = group["alternatives"][0]["url"]
            images.append(group)
        meta = source_metadata(meta)
        cards = catalogue_products(soup, url) if listing else []
        pagination = catalogue_pagination(soup, url) if not is_product else []
        links = [{'url':p['url'], 'product':True, 'pagination':False} for p in cards] + pagination
        return {'is_product':is_product, 'metadata':meta, 'images':images, 'products':cards,
                'links':links, 'pagination':pagination, 'page_number':page_number(url),
                'total_pages':max([page_number(url), *[p['number'] for p in pagination]]), 'adapter':self.name}



class WooCommerceAdapter(GenericAdapter):
    name = "woocommerce"

    def extract(self, html, url):
        result = super().extract(html, url)
        meta = result["metadata"]
        if result["is_product"]:
            # Remove retail condition and format suffixes; retain ambiguous title portions for review.
            parts = re.split(r"\s+[–—]\s+|\s+-\s+", meta["title"])
            kept = []
            for part in parts:
                if re.search(r"^(?:\(?condition|\(?\d{2}.\d{2}%|cover reprinted|CR$|CBF$|CTF$|(?:tamil\s+)?LP|(?:tamil\s+)?vinyl)", part, re.I):
                    continue
                if meta.get("artist") and part.strip().lower() == meta["artist"].strip().lower():
                    continue
                if meta.get("catalogue") and part.strip() == meta["catalogue"]:
                    continue
                if re.fullmatch(r"[A-Z]{0,6}\s*\d{3,}(?:\s+\d+)?", part):
                    meta.setdefault("catalogue", part)
                    continue
                kept.append(part)
            meta.setdefault("source_title", meta["title"])
            meta["title"] = " – ".join(kept).strip()
        return result


def adapter_for(html):
    return WooCommerceAdapter() if "woocommerce" in html.lower() or "wp-content" in html.lower() else GenericAdapter()


def render_page(url, fetcher):
    from .browser import browser_was_closed, BrowserSessionClosed
    for attempt in range(2):
        try:
            return _render_page_once(url, fetcher)
        except Exception as exc:
            if not browser_was_closed(exc):raise
            if attempt:
                raise BrowserSessionClosed('The JavaScript browser closed before this page finished. Discovery is paused at its saved page; resume to try a fresh browser session.') from exc


def _render_page_once(url, fetcher):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("JavaScript rendering requires: pip install '.[browser]' and python -m playwright install chromium") from exc
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, chromium_sandbox=True,
                                    args=["--disable-quic", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"])
        context = browser.new_context(service_workers="block", accept_downloads=False)
        context.add_init_script("for (const k of ['RTCPeerConnection','webkitRTCPeerConnection','WebTransport']) { try { Object.defineProperty(globalThis,k,{value:undefined,writable:false,configurable:false}); } catch {} }")
        context.route_web_socket("**/*", lambda ws: ws.close())
        def intercept(route):
            request = route.request
            if request.method != "GET" or request.resource_type in {"image", "media", "font"}:
                route.abort()
                return
            try:
                body, _ = fetcher.get(request.url)
                typ = "text/html" if request.resource_type == "document" else "application/javascript" if request.resource_type == "script" else "text/css" if request.resource_type == "stylesheet" else "application/json"
                route.fulfill(body=body, content_type=typ)
            except Exception:
                route.abort()
        context.route("**/*", intercept)
        try:
            page = context.new_page()
            page.goto(url, wait_until="networkidle", timeout=45000)
            page.wait_for_timeout(500)
            return page.content()
        finally:
            import contextlib
            with contextlib.suppress(Exception):context.close()
            with contextlib.suppress(Exception):browser.close()
