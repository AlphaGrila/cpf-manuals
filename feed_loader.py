"""
Feed loader (free, no AI) - Steve, 26 Nov 2026.
Loads whole catalogues from suppliers whose shop software publishes them:
  Shopify      <site>/products.json            (currency from <site>/cart.js)
  WooCommerce  <site>/wp-json/wc/store/v1/products
Rules agreed with Steve:
  * Include everything EXCEPT products we can be (near) certain are not relevant: gift cards, clothing,
    mugs, fees, deposits, donations, core charges... and parts that only name model years newer than the
    collector cut-off for an everyday car (performance, halo and exotic models stay - see scope.py).
  * Aftermarket and unbranded parts stay. The fit reader adds vehicles later; parts it can't tie to a
    vehicle are shown as "Universal" for text searches.
  * Approval: a feed part goes live once a free link check shows its product page exists (no AI review),
    because the data comes straight from the supplier's own store.
"""
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
import uuid
from concurrent.futures import ThreadPoolExecutor

from supabase_client import db
import scope as _scope

UA = {"User-Agent": "Mozilla/5.0 (compatible; CollectorPartsFinder/1.0; +https://collectorpartsfinder.com)",
      "Accept": "application/json"}
MAX_PER_SUPPLIER = int(os.environ.get("FL_MAX_PRODUCTS") or 10000)
DRY = bool(os.environ.get("FL_DRY"))

NOT_RELEVANT = re.compile(
    r"\b(gift ?cards?|gift certificates?|e-?gift|t-?shirts?|tee shirts?|hoodies?|sweat ?shirts?|apparel|clothing|"
    r"short[- ]sleeve|long[- ]sleeve|polo shirts?|caps? & hats|baseball caps?|trucker hats?|keyrings?|key ?chains?|"
    r"socks|beanies?|coffee mugs?|mugs?|candles?|donations?|memberships?|shipping (?:charge|fee|cost|insurance|upgrade)|"
    r"handling fee|deposits?|layaway|custom (?:order|invoice|payment)|payment link|service fee|labou?r charge|"
    r"core (?:charge|deposit|refund|return)|test product|do not (?:buy|purchase)|price adjustment)\b", re.I)
TOOL = re.compile(
    r"\b(creepers?|tool ?(?:kit|set|box|chest)|spanners?|wrench(?:es)?|socket sets?|screwdrivers?|pliers|hammers?|"
    r"multimeters?|inflators?|compressors?|(?:trolley|bottle|floor|scissor) jacks?|axle stands?|jack stands?|"
    r"torque wrench|puller|tap and die|drill bits?|grinders?|welders?|welding|soldering|"
    r"(?:workshop|garage) (?:light|lamp|equipment)|inspection lamps?|diagnostic|obd)\b", re.I)
SUPPLY = re.compile(
    r"\b(polish|wax|shampoo|cleaners?|degreasers?|sponges?|applicators?|microfib(?:re|er)|cloths?|"
    r"paints?|primers?|aerosols?|lacquer|thinners?|fillers?|sealants?|adhesives?|glue|tape|"
    r"engine oils?|gear oils?|lubricants?|grease|anti-?freeze|coolant|brake fluid|additives?|rust (?:remover|converter|treatment)|"
    r"sandpaper|abrasives?|masking)\b", re.I)


def item_type(name, text):
    """Tools and workshop supplies go to Tools & Supplies; everything else is a part."""
    n = f"{name} {text or ''}"
    if TOOL.search(n):
        return "tool"
    if SUPPLY.search(name):              # supplies by the product name only ("oil filter" stays a part)
        return "supply"
    return "part"


YEARS = re.compile(r"\b(19[0-9]{2}|20[0-4][0-9])\b")


def get(url, timeout=25, raw=False):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read(15_000_000)
        return body if raw else json.loads(body)


def clean(htmltext, n=1500):
    t = re.sub(r"<[^>]+>", " ", htmltext or "")
    t = html.unescape(re.sub(r"\s+", " ", t)).strip()
    return t[:n] or None


def too_new(text, sc):
    ys = [int(y) for y in YEARS.findall(text)]
    if not ys or min(ys) <= _scope.year_cutoff():
        return False
    low = text.lower()
    keep = any(m in re.sub(r"[^a-z0-9]", "", low) for _, m in sc.models) or \
        any(e in re.sub(r"[^a-z0-9]", "", low) for e in sc.exotic)
    return not keep                          # newer everyday car part only


def shopify(site):
    try:
        cur = (get(site + "/cart.js") or {}).get("currency") or "USD"
    except Exception:
        cur = "USD"
    page = 1
    while True:
        data = get(f"{site}/products.json?limit=250&page={page}")
        prods = (data or {}).get("products") or []
        for p in prods:
            vs = p.get("variants") or []
            v0 = next((v for v in vs if v.get("available")), vs[0] if vs else {})
            yield {
                "name": p.get("title"),
                "text": " | ".join(filter(None, [p.get("product_type"), " | ".join(p.get("tags") or []) if isinstance(p.get("tags"), list) else p.get("tags")])),
                "description": clean(p.get("body_html")),
                "skus": [v.get("sku") for v in vs if v.get("sku")],
                "price": v0.get("price"),
                "currency": cur,
                "in_stock": any(v.get("available") for v in vs) if vs else None,
                "image": ((p.get("images") or [{}])[0] or {}).get("src"),
                "url": f"{site}/products/{p.get('handle')}",
                "vendor": p.get("vendor"),
            }
        if len(prods) < 250 or page >= MAX_PER_SUPPLIER // 250:
            return
        page += 1
        time.sleep(0.6)


def woo(site):
    page = 1
    while True:
        prods = get(f"{site}/wp-json/wc/store/v1/products?per_page=100&page={page}") or []
        for p in prods:
            pr = p.get("prices") or {}
            minor = int(pr.get("currency_minor_unit") or 2)
            price = pr.get("price")
            yield {
                "name": clean(p.get("name"), 300),
                "text": " | ".join([c.get("name", "") for c in p.get("categories") or []] + [t.get("name", "") for t in p.get("tags") or []]),
                "description": clean(p.get("short_description") or p.get("description")),
                "skus": [p.get("sku")] if p.get("sku") else [],
                "price": (int(price) / (10 ** minor)) if price and str(price).isdigit() else None,
                "currency": pr.get("currency_code") or "USD",
                "in_stock": p.get("is_in_stock"),
                "image": ((p.get("images") or [{}])[0] or {}).get("src"),
                "url": p.get("permalink"),
                "vendor": None,
            }
        if len(prods) < 100 or page * 100 >= MAX_PER_SUPPLIER:
            return
        page += 1
        time.sleep(0.6)


def page_exists(url):
    """Free link check. Shops throttle fast visitors (429), so back off and retry instead of failing the part."""
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, headers={**UA, "Accept": "text/html"}, method="GET")
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status == 200
        except urllib.error.HTTPError as e:
            if e.code in (429, 503, 502, 520, 522):
                time.sleep(2 + attempt * 4)
                continue
            return False
        except Exception:
            time.sleep(1)
    return False


def load_supplier(feed, sup, known_urls, sc, stats):
    site, kind = feed["site"], feed["kind"]
    items, skipped = [], {"not relevant": 0, "newer car only": 0, "no name or link": 0, "already loaded": 0}
    try:
        for it in (shopify(site) if kind == "shopify" else woo(site)):
            name = (it.get("name") or "").strip()
            if not name or not it.get("url"):
                skipped["no name or link"] += 1
                continue
            if it["url"] in known_urls:
                skipped["already loaded"] += 1
                continue
            blob = f"{name} {it.get('text') or ''}"
            if NOT_RELEVANT.search(blob):
                skipped["not relevant"] += 1
                continue
            if too_new(f"{name} {it.get('text') or ''}", sc):
                skipped["newer car only"] += 1
                continue
            items.append(it)
            if len(items) >= MAX_PER_SUPPLIER:
                break
    except Exception as e:
        print(f"  {sup['name']}: feed stopped early ({str(e)[:80]})")
    live = None
    if kind == "shopify" and len(items) > 40:
        # The shop's own products.json only lists published products, but Shopify blocks a fast
        # page-by-page check after ~60 pages. So check an even sample slowly; if 90%+ are fine, keep all.
        step = len(items) / 25
        sample = [items[int(i * step)] for i in range(25)]
        good = 0
        for it in sample:
            good += 1 if page_exists(it["url"]) else 0
            time.sleep(1.5)
        print(f"  {sup['name']}: sample check {good}/25 pages OK")
        if good >= 23:
            live = items
    if live is None:
        with ThreadPoolExecutor(2 if kind == "shopify" else 4) as ex:
            ok = list(ex.map(lambda it: page_exists(it["url"]), items[:]))
        live = [it for it, good in zip(items, ok) if good]
    print(f"  {sup['name']} ({kind}): {len(items)} kept, {len(live)} pages checked OK; skipped {skipped}")
    stats["kept"] += len(live)
    for k, v in skipped.items():
        stats[k] = stats.get(k, 0) + v
    if DRY or not live and not db.select("staging_parts", "id", {"supplier_id": f"eq.{sup['id']}", "source": "eq.supplier_feed", "promoted_at": "is.null"}, limit=1):
        for it in live[:8]:
            print(f"     e.g. [{item_type(it['name'], it.get('text'))}] {it['name'][:70]} | {it.get('price')} {it.get('currency')}")
        return 0
    waiting = {r["source_url"] for r in db.select("staging_parts", "source_url",
               {"supplier_id": f"eq.{sup['id']}", "source": "eq.supplier_feed", "promoted_at": "is.null"}, limit=50000)}
    rows = []
    for it in live:
        if it["url"] in waiting:            # saved by an earlier run that stopped part-way: just promote it
            continue
        rows.append({
            "id": str(uuid.uuid4()), "name": it["name"][:300], "description": it.get("description"),
            "category_text": (it.get("text") or "")[:300] or None,
            "item_type": item_type(it["name"], it.get("text")),
            "brand_text": None, "model_text": None,
            "part_numbers": it["skus"][:10],
            "specifications": {"price": it.get("price"), "currency": it.get("currency"),
                               "in_stock": it.get("in_stock"), "image_url": it.get("image"),
                               "vendor": it.get("vendor"), "feed_tags": (it.get("text") or "")[:500]},
            "image_urls": [it["image"]] if it.get("image") else [],
            "source_url": it["url"], "confidence": 0.9, "validation_status": "passed",
            "supplier_name": sup["name"], "supplier_id": sup["id"], "source": "supplier_feed",
        })
    for i in range(0, len(rows), 200):
        db.insert("staging_parts", rows[i:i + 200])
    done = failed = 0
    for _ in range(600):
        res = None
        for attempt in range(4):
            try:
                res = db.rpc("cpf_promote_feed", {"p_supplier_id": sup["id"], "p_limit": 100}) or {}
                break
            except Exception as e:
                print(f"     promote slow ({str(e)[:60]}), retrying")
                time.sleep(10 * (attempt + 1))
        if res is None:
            print("     promote kept failing; the rest will be picked up next run")
            break
        done += res.get("promoted", 0)
        failed += res.get("failed", 0)
        if not res.get("left") or not (res.get("promoted") or res.get("failed")):
            break
    try:
        db.rpc("cpf_feed_loaded", {"p_supplier_id": sup["id"], "p_n": len(rows)})
    except Exception:
        pass                                   # returns no body
    print(f"     live: {done} parts ({failed} could not be promoted)")
    return len(rows)


def main():
    sc = _scope.load(db)
    limit = int(os.environ.get("FL_SUPPLIERS") or 20)
    only = os.environ.get("FL_ONLY")
    feeds = db.select("supplier_feeds", "supplier_id,site,kind,products,last_loaded_at,loaded_products",
                      {"kind": "not.is.null", "order": "products.asc"}, limit=5000)
    if os.environ.get("FL_RELOAD"):    # shops that loaded short (throttled link checks)
        feeds = [f for f in feeds if f.get("last_loaded_at")
                 and (f.get("loaded_products") or 0) < 0.9 * (f.get("products") or 0)]
    else:
        feeds = [f for f in feeds if not f.get("last_loaded_at")]
    small = int(os.environ.get("FL_SMALL") or 0)
    if small:                          # smallest catalogues first (most suppliers for the database space)
        feeds = [f for f in feeds if 20 <= (f.get("products") or 0) <= small]
    if only:
        feeds = [f for f in feeds if only.lower() in f["site"].lower()]
    elif not os.environ.get("FL_RELOAD") and not small:   # pilot: a spread of small, middle and large catalogues
        feeds = [f for f in feeds if (f.get("products") or 0) >= 20]
        if len(feeds) > limit:
            step = len(feeds) / limit
            feeds = [feeds[int(i * step)] for i in range(limit)]
    sups = {s["id"]: s for s in db.select("suppliers", "id,name", {"active": "eq.true"}, limit=20000)}
    known = {r["url"] for r in db.select("part_listings", "url", limit=200000) if r.get("url")}
    stats = {"kept": 0}
    total = 0
    for f in feeds[:limit]:
        if f["supplier_id"] in sups:
            total += load_supplier(f, sups[f["supplier_id"]], known, sc, stats)
    print(f"\nFeed loader: {total} parts saved{' (dry run: nothing saved)' if DRY else ''}. Totals: {stats}")


if __name__ == "__main__":
    sys.exit(main())
