"""Free site probe for suppliers without a Shopify / WooCommerce feed (no AI).

For each site: which shop system it runs, whether it publishes a site map of product pages, and whether its
product pages carry standard schema.org Product data (name, price, stock) that can be read without AI.
Results go to the supplier_probe table. Steve 27 Nov: find the free route before paying for AI page reading.
"""
import gzip
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from supabase_client import db

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
LIMIT = int(os.environ.get("SP_LIMIT") or 1200)
THREADS = int(os.environ.get("SP_THREADS") or 12)

PLATFORMS = [
    ("bigcommerce", r"cdn\d*\.bigcommerce\.com|bigcommerce"),
    ("squarespace", r"static1\.squarespace\.com|squarespace"),
    ("wix", r"wixstatic\.com|wix\.com"),
    ("magento", r"/mage/|magento|/static/version\d+/frontend|mage-cache"),
    ("prestashop", r"prestashop"),
    ("opencart", r"route=product/|opencart"),
    ("ecwid", r"ecwid"),
    ("volusion", r"volusion"),
    ("shift4shop", r"3dcart|shift4shop"),
    ("weebly", r"weebly|editmysite"),
    ("godaddy", r"godaddy|img1\.wsimg\.com"),
    ("nopcommerce", r"nopcommerce"),
    ("shopify", r"cdn\.shopify\.com"),
    ("woocommerce", r"woocommerce|wp-content/plugins/woocommerce"),
    ("wordpress", r"wp-content"),
]
PRODUCT_URL = re.compile(r"/(products?|p|item|items|shop|store|catalog|parts?)/[^/?#]+|[-_/](p|pd|prod|product)[-_]?\d+|\.html?$", re.I)


def get(url, limit=1_500_000, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(limit)
        if r.headers.get("Content-Encoding") == "gzip" or url.endswith(".gz"):
            try:
                data = gzip.decompress(data)
            except Exception:
                pass
        return r.geturl(), data.decode("utf-8", "replace")


def sitemap_urls(base, home_html):
    """Collect page URLs from robots.txt sitemaps / sitemap.xml (following one level of sitemap indexes)."""
    maps = []
    try:
        _, robots = get(urllib.parse.urljoin(base, "/robots.txt"), 200_000)
        maps += re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots)
    except Exception:
        pass
    maps += [urllib.parse.urljoin(base, p) for p in ("/sitemap.xml", "/sitemap_index.xml", "/xmlsitemap.php", "/sitemap.php")]
    seen, urls = set(), []
    queue = list(dict.fromkeys(maps))
    while queue and len(seen) < 25:
        m = queue.pop(0)
        if m in seen:
            continue
        seen.add(m)
        try:
            _, x = get(m, 4_000_000)
        except Exception:
            continue
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", x)
        if "<sitemapindex" in x:
            # product sitemaps first
            locs.sort(key=lambda u: 0 if re.search(r"product|item|catalog|shop", u, re.I) else 1)
            queue += locs[:20]
        else:
            urls += locs
        if len(urls) > 200_000:
            break
    return urls


def product_data(html):
    """schema.org Product in JSON-LD (or microdata): returns (is_product, has_price)."""
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except Exception:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for it in items:
            if not isinstance(it, dict):
                continue
            t = it.get("@type")
            if t == "Product" or (isinstance(t, list) and "Product" in t):
                offers = it.get("offers") or {}
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                if not isinstance(offers, dict):
                    offers = {}
                ps = offers.get("priceSpecification") or {}
                if isinstance(ps, list):
                    ps = ps[0] if ps else {}
                price = offers.get("price") or offers.get("lowPrice") or (ps.get("price") if isinstance(ps, dict) else None)
                return True, price not in (None, "", "0", 0)
    if re.search(r'itemtype=["\']https?://schema\.org/Product["\']', html, re.I):
        return True, bool(re.search(r'itemprop=["\']price["\']', html, re.I))
    return False, False


def probe(row):
    try:
        return _probe(row)
    except Exception as e:                       # one odd site must never stop the run
        return {"supplier_id": row["supplier_id"], "site": row.get("site"), "reachable": None, "platform": None,
                "sitemap_urls": 0, "product_urls": 0, "jsonld_checked": 0, "jsonld_ok": 0, "jsonld_price": 0,
                "sample_url": None, "notes": f"probe error: {str(e)[:80]}"}


def _probe(row):
    site = (row.get("site") or "").strip()
    out = {"supplier_id": row["supplier_id"], "site": site, "reachable": False, "platform": None,
           "sitemap_urls": 0, "product_urls": 0, "jsonld_checked": 0, "jsonld_ok": 0, "jsonld_price": 0,
           "sample_url": None, "notes": None}
    if not site:
        out["notes"] = "no website on file"
        return out
    if not site.startswith("http"):
        site = "https://" + site
    try:
        final, home = get(site, 600_000)
    except Exception as e:
        out["notes"] = f"home page: {str(e)[:80]}"
        return out
    out["reachable"] = True
    base = "{0.scheme}://{0.netloc}".format(urllib.parse.urlparse(final))
    low = home.lower()
    for name, rx in PLATFORMS:
        if re.search(rx, low):
            out["platform"] = name
            break
    urls = sitemap_urls(base, home)
    host = urllib.parse.urlparse(base).netloc.replace("www.", "")
    urls = [u for u in urls if host in u]
    prods = [u for u in urls if PRODUCT_URL.search(urllib.parse.urlparse(u).path)]
    out["sitemap_urls"], out["product_urls"] = len(urls), len(prods)
    # sample product pages (or links from the home page when there is no site map)
    sample = random.sample(prods, min(3, len(prods))) if prods else []
    if not sample:
        links = re.findall(r'href=["\']([^"\'#]+)["\']', home)
        links = [urllib.parse.urljoin(base, l) for l in links]
        sample = [l for l in dict.fromkeys(links) if host in l and PRODUCT_URL.search(urllib.parse.urlparse(l).path)][:3]
    for u in sample:
        try:
            _, page = get(u, 1_000_000)
        except Exception:
            continue
        out["jsonld_checked"] += 1
        ok, price = product_data(page)
        out["jsonld_ok"] += int(ok)
        out["jsonld_price"] += int(ok and price)
        if ok and not out["sample_url"]:
            out["sample_url"] = u
        time.sleep(0.5)
    return out


def main():
    rows = db.rpc("cpf_probe_todo", {"p_limit": min(LIMIT, 1000)}) or []      # API returns 1,000 rows at most
    print(f"{len(rows)} suppliers to probe", flush=True)
    done = 0
    with ThreadPoolExecutor(THREADS) as ex:
        for res in ex.map(probe, rows[:LIMIT]):
            try:
                db.insert("supplier_probe", res)
            except Exception as e:
                print("save failed:", str(e)[:120])
            done += 1
            if done % 50 == 0:
                print(f"  {done} probed", flush=True)
    print("done")


if __name__ == "__main__":
    sys.exit(main())
