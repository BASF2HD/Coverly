from __future__ import annotations

import os
from urllib.parse import urlsplit, urlencode, urljoin
import json
from bs4 import BeautifulSoup
from .network import SafeHTTP, canonical_url
from .intelligence import identify


def media_type(text):
    text = text.lower()
    if any(x in text for x in ("cassette", "audio cd", "compact disc", "poster")):
        return "other"
    return "lp" if any(x in text for x in ("vinyl", "lp record", "sleeve", "soundtrack")) else "unverified"


def search(settings, query, purpose="artwork"):
    suffix=" Tamil movie music director composer year soundtrack" if purpose=="metadata" else " Tamil vinyl LP cover"
    if settings.search_provider == "brave":
        key = os.environ.get("BRAVE_SEARCH_API_KEY")
        if not key:
            raise ValueError("Brave search needs BRAVE_SEARCH_API_KEY in the local environment")
        endpoint = "https://api.search.brave.com/res/v1/web/search?" + urlencode({"q": query + suffix, "count": 20})
        fetcher = SafeHTTP(settings, {"api.search.brave.com"})
        try:
            raw, _, _ = fetcher._request(endpoint, 3 * 1024 * 1024, headers={"X-Subscription-Token": key, "Accept": "application/json"})
            payload = json.loads(raw)
        except Exception:
            raise ValueError("Brave Search API request failed; verify local credentials and quota") from None
        finally:
            fetcher.close()
        return [{"url": canonical_url(item["url"]), "title": item["title"], "preview": item.get("thumbnail", {}).get("src"),
                 "source": urlsplit(item["url"]).hostname, "dimensions": None, "confidence": .55,
                 "media_type": media_type(item["title"] + " " + item.get("description", ""))}
                for item in payload.get("web", {}).get("results", [])]
    if settings.search_provider == "google":
        key, cx = os.environ.get("GOOGLE_SEARCH_API_KEY"), os.environ.get("GOOGLE_SEARCH_CX")
        if not key or not cx:
            raise ValueError("Google search needs GOOGLE_SEARCH_API_KEY and GOOGLE_SEARCH_CX in the local environment")
        endpoint = "https://www.googleapis.com/customsearch/v1?" + urlencode({"key": key, "cx": cx, "q": query + suffix, "num": 10})
        fetcher = SafeHTTP(settings, {"www.googleapis.com"})
        try:
            raw, _, _ = fetcher._request(endpoint, 3 * 1024 * 1024)
            payload = json.loads(raw)
        except Exception as exc:
            raise ValueError("Search API request failed; verify API credentials and quota") from None
        finally:
            fetcher.close()
        results = []
        for item in payload.get("items", []):
            images = item.get("pagemap", {}).get("cse_image", [])
            results.append({"url": canonical_url(item["link"]), "title": item["title"], "preview": images[0].get("src") if images else None,
                            "source": urlsplit(item["link"]).hostname, "dimensions": None,
                            "confidence": .55, "media_type": media_type(item["title"] + " " + item.get("snippet", ""))})
        return results
    base = canonical_url(settings.search_site)
    parts = urlsplit(base)
    url = f"{parts.scheme}://{parts.netloc}/?" + urlencode({"s": query + " Tamil vinyl", "post_type": "product"})
    fetcher = SafeHTTP(settings, {parts.hostname})
    try:
        raw, final = fetcher.get(url)
    finally:
        fetcher.close()
    soup = BeautifulSoup(raw, "html.parser")
    results = []
    for card in soup.select(".products > .product, li.product, article.product, .product-small")[:48]:
        anchor = card.select_one(".wd-entities-title a, a.woocommerce-LoopProduct-link, a.woocommerce-loop-product__link, a.product-image-link, a[href*='/product/']")
        heading = card.select_one("h2, h3, .product-title")
        if not anchor:
            continue
        title = heading.get_text(" ", strip=True) if heading else anchor.get_text(" ", strip=True)
        image = card.select_one("img")
        preview = urljoin(final, image.get("data-src") or image.get("src") or "") if image else None
        dest = canonical_url(urljoin(final, anchor["href"]))
        results.append({"url": dest, "title": title, "preview": preview, "source": parts.hostname,
                        "dimensions": None, "confidence": .7, "media_type": media_type(title)})
    return list({r["url"]: r for r in results}.values())
