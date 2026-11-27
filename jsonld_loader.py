"""Free catalogue loader for supplier sites that publish standard product data (no AI).

Step 10b (Steve 27 Nov): for suppliers without a Shopify / WooCommerce feed whose product pages carry schema.org
Product data (name, price, stock, image) and whose site map lists the product pages. Each product page is read
once; the page loading proves it exists, so no separate link check. Same filters as the feed loader (not relevant,
newer car only, tools / supplies), same staging -> live promotion, then vehicle matching overnight.
"""
import html
import json
import os
import re
import sys
import time
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor

from supabase_client import db
import scope as _scope
import feed_loader as fl
import site_probe as sp

PER_RUN = int(os.environ.get("JL_PER_SUPPLIER") or 4000)     # pages per supplier per run (big sites resume next run)
SUPPLIERS = int(os.environ.get("JL_SUPPLIERS") or 200)
DRY = bool(os.environ.get("JL_DRY"))
DEADLINE = time.time() + 60 * float(os.environ.get("JL_MINUTES") or 320)


def as_list(x):
    return x if isinstance(x, list) else [x] if x else []


SKIP_LINK = re.compile(r"\.(jpe?g|png|gif|webp|svg|pdf|zip|css|js|ico|mp4)(\?|$)|/(cart|checkout|account|login|"
                       r"my-account|wishlist|compare|search|basket|customer|cdn-cgi)\b|[?&](sort|order|filter|limit|dir)=", re.I)


def read_product(url, links=None):
    """Fetch one page; return the product item dict or None. With links=list, also collect the page's links."""
    try:
        final, page = sp.get(url, 1_500_000)
    except Exception:
        return None
    if links is not None:
        links.extend(urllib.parse.urljoin(final, html.unescape(h).split("#")[0])
                     for h in re.findall(r'href=["\']([^"\']+)', page))
    return parse(page, final)


def parse(page, final):
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except Exception:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for it in items:
            if not isinstance(it, dict):
                continue
            t = it.get("@type")
            if not (t == "Product" or (isinstance(t, list) and "Product" in t)):
                continue
            offers = it.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if not isinstance(offers, dict):
                offers = {}
            ps = offers.get("priceSpecification") or {}
            if isinstance(ps, list):
                ps = ps[0] if ps else {}
            price = offers.get("price") or offers.get("lowPrice") or (ps.get("price") if isinstance(ps, dict) else None)
            try:
                price = float(str(price).replace(",", "")) if price not in (None, "") else None
            except ValueError:
                price = None
            if not price or price <= 0:        # never show a $0 price
                price = None
            avail = str(offers.get("availability") or "").lower()
            img = it.get("image")
            if isinstance(img, list):
                img = img[0] if img else None
            if isinstance(img, dict):
                img = img.get("url")
            brand = it.get("brand")
            if isinstance(brand, dict):
                brand = brand.get("name")
            cat = it.get("category")
            if isinstance(cat, list):
                cat = " | ".join(str(c) for c in cat)
            skus = [str(s).strip() for s in as_list(it.get("sku")) + as_list(it.get("mpn")) if str(s).strip()]
            name = html.unescape(str(it.get("name") or "")).strip()
            if not name:
                return None
            return {"name": name, "url": final, "price": price,
                    "currency": (offers.get("priceCurrency") or (ps.get("priceCurrency") if isinstance(ps, dict) else None) or "")[:3] or None,
                    "in_stock": None if not avail else ("instock" in avail or "limitedavailability" in avail),
                    "image": img if isinstance(img, str) else None,
                    "description": fl.clean(str(it.get("description") or ""), 1500),
                    "text": " | ".join(x for x in (str(cat) if cat else "", str(brand) if brand else "") if x),
                    "vendor": str(brand)[:100] if brand else None, "skus": list(dict.fromkeys(skus))[:10]}
    return None


def load(sup, probe, known, sc, stats):
    site = probe["site"] if probe["site"].startswith("http") else "https://" + probe["site"]
    try:
        final, home = sp.get(site, 600_000)
    except Exception as e:
        print(f"  {sup['name']}: home page failed ({str(e)[:60]})")
        return 0
    base = "{0.scheme}://{0.netloc}".format(urllib.parse.urlparse(final))
    host = urllib.parse.urlparse(base).netloc.replace("www.", "")
    urls = [u for u in sp.sitemap_urls(base, home) if host in u]
    prods = [u for u in dict.fromkeys(urls) if sp.PRODUCT_URL.search(urllib.parse.urlparse(u).path) and u not in known]
    skipped = {"not relevant": 0, "newer car only": 0, "not a product page": 0}
    items = []
    if len(prods) >= 5:
        todo = prods[:PER_RUN]

        def one(u):
            if time.time() > DEADLINE:
                return None
            it = read_product(u)
            time.sleep(0.4)
            return it

        with ThreadPoolExecutor(4) as ex:
            found = list(ex.map(one, todo))
    else:                                  # no usable site map: walk the shop's own links from the home page
        found, todo, seen, queue = [], [], {final}, [final]
        while queue and len(todo) < PER_RUN and time.time() <= DEADLINE:
            batch, queue = queue[:8], queue[8:]
            outs = []

            def walk(u):
                ls = []
                it = read_product(u, ls)
                time.sleep(0.4)
                return it, ls

            with ThreadPoolExecutor(4) as ex:
                outs = list(ex.map(walk, batch))
            todo += batch
            for it, ls in outs:
                if it and it["url"] not in known:
                    found.append(it)
                for l in ls:
                    pu = urllib.parse.urlparse(l)
                    if pu.netloc.replace("www.", "") == host and l not in seen and not SKIP_LINK.search(l):
                        seen.add(l)
                        queue.append(l)
        prods = todo + queue
        print(f"  {sup['name']}: no site map list, walked {len(todo)} pages", flush=True)
    if True:
        for it in found:
            if not it:
                skipped["not a product page"] += 1
                continue
            blob = f"{it['name']} {it.get('text') or ''}"
            if fl.NOT_RELEVANT.search(blob):
                skipped["not relevant"] += 1
                continue
            if fl.too_new(blob, sc):
                skipped["newer car only"] += 1
                continue
            items.append(it)
    finished = len(prods) <= PER_RUN and time.time() <= DEADLINE
    print(f"  {sup['name']}: {len(prods)} product pages, read {len(todo)}, kept {len(items)}; skipped {skipped}"
          f"{'' if finished else ' (more next run)'}", flush=True)
    stats["kept"] += len(items)
    if DRY:
        for it in items[:6]:
            print(f"     e.g. [{fl.item_type(it['name'], it.get('text'))}] {it['name'][:70]} | {it.get('price')} {it.get('currency')} | {it.get('skus')[:2]}")
        return 0
    rows = [{
        "id": str(uuid.uuid4()), "name": it["name"][:300], "description": it.get("description"),
        "category_text": (it.get("text") or "")[:300] or None,
        "item_type": fl.item_type(it["name"], it.get("text")),
        "brand_text": None, "model_text": None, "part_numbers": it["skus"],
        "specifications": {"price": it.get("price"), "currency": it.get("currency"), "in_stock": it.get("in_stock"),
                           "image_url": it.get("image"), "vendor": it.get("vendor"), "feed_tags": (it.get("text") or "")[:500],
                           "read_from": "schema.org product data"},
        "image_urls": [it["image"]] if it.get("image") else [],
        "source_url": it["url"], "confidence": 0.9, "validation_status": "passed",
        "supplier_name": sup["name"], "supplier_id": sup["id"], "source": "supplier_feed",
    } for it in items]
    for i in range(0, len(rows), 200):
        db.insert("staging_parts", rows[i:i + 200])
    done = 0
    for _ in range(600):
        res = None
        for attempt in range(4):
            try:
                res = db.rpc("cpf_promote_feed", {"p_supplier_id": sup["id"], "p_limit": 100}) or {}
                break
            except Exception as e:
                time.sleep(10 * (attempt + 1))
        if res is None:
            break
        done += res.get("promoted", 0)
        if not res.get("left") or not (res.get("promoted") or res.get("failed")):
            break
    if finished:
        db.update("supplier_probe", {"loaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                  {"supplier_id": f"eq.{sup['id']}"})
    print(f"     live: {done} parts", flush=True)
    return done


def main():
    sc = _scope.load(db)
    probes = db.select("supplier_probe", "supplier_id,site,product_urls,jsonld_price,loaded_at",
                       {"jsonld_price": "gte.2", "loaded_at": "is.null", "order": "product_urls.asc"}, limit=2000)
    sups = {s["id"]: s for s in db.select("suppliers", "id,name", {"active": "eq.true"}, limit=20000)}
    known = {r["url"] for r in db.select("part_listings", "url", limit=500000) if r.get("url")}
    print(f"{len(probes)} readable suppliers waiting", flush=True)
    stats, total = {"kept": 0}, 0
    for p in probes[:SUPPLIERS]:
        if time.time() > DEADLINE:
            print("time limit reached; continuing next run")
            break
        if p["supplier_id"] in sups:
            try:
                total += load(sups[p["supplier_id"]], p, known, sc, stats)
            except Exception as e:
                print(f"  {sups[p['supplier_id']]['name']}: stopped ({str(e)[:80]})")
    print(f"\nFree product-data loader: {total} parts live{' (dry run)' if DRY else ''}. Kept {stats['kept']}.")


if __name__ == "__main__":
    sys.exit(main())
