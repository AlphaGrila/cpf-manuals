"""AI page reader for suppliers whose pages carry no standard product data (Option A, Steve 27 Nov: free only).

Reads product pages with Gemini on the free key. Free only: no paid backup, and the run stops for the day as soon
as Google's free allowance is used up (it carries on the next night from where it stopped).

Accuracy guards (never guess prices, part numbers or links):
  - the AI is told to copy only what the page says;
  - a price is kept only if that number appears in the page text, otherwise "See price at supplier";
  - a part number is kept only if it appears in the page text;
  - a product link is kept only if it is a real link on that page (otherwise the page itself).
Same filters as the other loaders (not relevant, newer car only), same staging -> live path, then vehicle matching.
"""
import html
import json
import os
import re
import sys
import time
import urllib.parse
import uuid

from supabase_client import db
import scope as _scope
import feed_loader as fl
import site_probe as sp
import gemini_client as gem

DAY_PAGES = int(os.environ.get("AR_PAGES") or 900)          # pages per night (free allowance)
PER_SITE = int(os.environ.get("AR_PER_SITE") or 150)        # pages per supplier per night, so many suppliers move
GAP = float(os.environ.get("AR_GAP") or 7)                # seconds between AI calls (stay under the per-minute limit)
DRY = bool(os.environ.get("AR_DRY"))
ONLY = os.environ.get("AR_ONLY")
DEADLINE = time.time() + 60 * float(os.environ.get("AR_MINUTES") or 300)

SYSTEM = """You read one web page from a classic / collector vehicle parts shop and list the parts for sale on it.
Copy ONLY what the page text says. Never guess or invent anything.
Return JSON: {"items": [{"name": "...", "part_numbers": ["..."], "price": 12.5 or null, "currency": "USD" or null,
"in_stock": true/false/null, "vehicles": "makes, models and years the page says it fits, or null",
"link": "the product's own link if the page lists several products, else null"}]}
Rules:
- One entry per part offered for sale. A page with no parts for sale (home, about, blog, category with no
  products) gives {"items": []}.
- price: the selling price as a plain number, only if shown next to that part. Otherwise null.
- currency: the 3-letter code if the page shows it or a clear symbol (e.g. $ on a US shop = USD, £ = GBP, € = EUR).
- part_numbers: only codes printed on the page for that part (SKU, part no., OEM no.). Otherwise [].
- link: copy it exactly from the links list given; never make one up.
- At most 40 items."""

STRIP = re.compile(r"(?is)<(script|style|noscript|svg|header|footer|nav|form)[^>]*>.*?</\1>")


def page_text(page, final):
    links = {}
    for href, label in re.findall(r'(?is)<a[^>]+href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>', page):
        u = urllib.parse.urljoin(final, html.unescape(href))
        t = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", label))).strip()
        if t and len(t) > 3 and urllib.parse.urlparse(u).netloc == urllib.parse.urlparse(final).netloc \
                and not re.search(r"(?i)/(cart|checkout|account|login|wishlist|compare)\b", u):
            links.setdefault(u, t[:80])
    title = re.search(r"(?is)<title[^>]*>(.*?)</title>", page)
    img = re.search(r'(?i)<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', page)
    t = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", STRIP.sub(" ", page)))).strip()
    return (html.unescape(title.group(1)).strip() if title else ""), t[:9000], links, (img.group(1) if img else None)


def numbers_in(text):
    return {re.sub(r"[^\d.]", "", n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d{1,2})?", text)}


def words(t):
    return {w for w in re.findall(r"[a-z0-9]+", (t or "").lower()) if len(w) > 2}


def checked(item, text, links, page_url, title=""):
    """Apply the accuracy guards; return the cleaned item or None."""
    name = html.unescape(str(item.get("name") or "")).strip()
    if len(name) < 3 or name.split()[0].lower()[:6] not in text.lower():
        return None                                     # the name must really be on the page
    price = item.get("price")
    try:
        price = round(float(price), 2) if price not in (None, "") else None
    except (TypeError, ValueError):
        price = None
    if price is not None:
        nums = numbers_in(text)
        if price <= 0 or not ({f"{price:.2f}", f"{price:g}", str(int(price)) if price == int(price) else ""} & nums):
            price = None                                # not printed on the page: show "See price at supplier"
    low = text.lower().replace(" ", "")
    pns = [str(p).strip() for p in (item.get("part_numbers") or []) if str(p).strip()]
    pns = [p for p in pns if p.lower().replace(" ", "") in low][:10]
    link = item.get("link")
    if link in links:
        url = link
    else:                                               # no link of its own: only the page's main product may use the page link
        nw, tw = words(name), words(title)
        if not nw or len(nw & tw) / len(nw) < 0.5:
            return None                                 # e.g. "related products" on another product's page
        url = page_url
    cur = (item.get("currency") or "")[:3].upper() or None
    return {"name": name[:300], "price": price, "currency": cur if price is not None else cur,
            "in_stock": item.get("in_stock") if isinstance(item.get("in_stock"), bool) else None,
            "vehicles": (item.get("vehicles") or "")[:300] or None, "skus": pns, "url": url}


def read_site(probe, sup, known, sc, budget, stats):
    site = probe["site"] if probe["site"].startswith("http") else "https://" + probe["site"]
    try:
        final, home = sp.get(site, 600_000)
    except Exception as e:
        print(f"  {sup['name']}: home page failed ({str(e)[:60]})", flush=True)
        db.update("supplier_probe", {"ai_done_at": now()}, {"supplier_id": f"eq.{sup['id']}"})
        return 0
    base = "{0.scheme}://{0.netloc}".format(urllib.parse.urlparse(final))
    host = urllib.parse.urlparse(base).netloc.replace("www.", "")
    urls = [u for u in dict.fromkeys(sp.sitemap_urls(base, home)) if host in u
            and sp.PRODUCT_URL.search(urllib.parse.urlparse(u).path)]
    start = probe.get("ai_offset") or 0
    todo, skipped_new, pos = [], 0, start
    for u in urls[start:]:
        if len(todo) >= min(PER_SITE, budget):
            break
        pos += 1
        if u in known:
            continue
        if fl.too_new(urllib.parse.unquote(u).replace("-", " ").replace("_", " "), sc):
            skipped_new += 1                            # e.g. /2018-ram-promaster/ : newer car, no AI spent
            continue
        todo.append(u)
    items, calls = [], 0
    for u in todo:
        if time.time() > DEADLINE or stats["stop"]:
            break
        try:
            pfinal, page = sp.get(u, 1_500_000)
        except Exception:
            continue
        title, text, links, img = page_text(page, pfinal)
        if len(text) < 80:
            continue
        user = f"Page: {pfinal}\nTitle: {title}\n\nLinks on the page:\n" + \
               "\n".join(f"{k} | {v}" for k, v in list(links.items())[:60]) + f"\n\nPage text:\n{text}"
        t0 = time.time()
        try:
            out = json.loads(gem.message(SYSTEM, user, 3000))
            stats["fails"] = 0
        except gem.GeminiUnavailable as e:
            stats["fails"] += 1
            print(f"     AI unavailable ({e})", flush=True)
            if stats["fails"] >= 5:
                stats["stop"] = True                    # free allowance used up: stop, carry on next night
                print("  Free AI allowance used up for today - stopping; continues next run.", flush=True)
            time.sleep(65)                              # per-minute limit: wait a minute and go on
            continue
        except Exception:
            out = {}
        calls += 1
        stats["pages"] += 1
        found = 0
        for it in (out.get("items") or [])[:40] if isinstance(out, dict) else []:
            c = checked(it, text, links, pfinal, title) if isinstance(it, dict) else None
            if not c or c["url"] in known:
                continue
            blob = f"{c['name']} {c.get('vehicles') or ''}"
            if fl.NOT_RELEVANT.search(blob):
                stats["not relevant"] += 1
                continue
            if fl.too_new(blob, sc):
                stats["newer car only"] += 1
                continue
            c["image"] = img if c["url"] == pfinal else None
            known.add(c["url"]) if c["url"] != pfinal else None
            items.append(c)
            found += 1
        time.sleep(max(0, GAP - (time.time() - t0)))
    finished = pos >= len(urls)
    print(f"  {sup['name']}: {len(urls)} product pages listed; AI read {calls} (from #{start}); "
          f"kept {len(items)}; newer-car pages skipped {skipped_new}{'' if finished else ' (more next run)'}",
          flush=True)
    stats["kept"] += len(items)
    if DRY:
        for it in items[:8]:
            print(f"     e.g. {it['name'][:70]} | {it['price']} {it['currency']} | {it['skus'][:2]} | {it['url'][:70]}")
        return calls
    # one row per (name, url): a product page gives one part; a list page can give several
    seen, rows = set(), []
    for it in items:
        key = (it["name"].lower(), it["url"])
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "id": str(uuid.uuid4()), "name": it["name"], "description": None,
            "category_text": it.get("vehicles"), "item_type": fl.item_type(it["name"], it.get("vehicles")),
            "brand_text": None, "model_text": None, "part_numbers": it["skus"],
            "specifications": {"price": it["price"], "currency": it["currency"], "in_stock": it["in_stock"],
                               "image_url": it.get("image"), "vendor": None, "feed_tags": it.get("vehicles"),
                               "read_from": "AI page reader (Gemini free)"},
            "image_urls": [it["image"]] if it.get("image") else [],
            "source_url": it["url"], "confidence": 0.75, "validation_status": "passed",
            "supplier_name": sup["name"], "supplier_id": sup["id"], "source": "supplier_feed"})
    for i in range(0, len(rows), 200):
        db.insert("staging_parts", rows[i:i + 200])
    live = 0
    for _ in range(200):
        res = None
        for attempt in range(4):
            try:
                res = db.rpc("cpf_promote_feed", {"p_supplier_id": sup["id"], "p_limit": 100}) or {}
                break
            except Exception:
                time.sleep(10 * (attempt + 1))
        if res is None:
            break
        live += res.get("promoted", 0)
        if not res.get("left") or not (res.get("promoted") or res.get("failed")):
            break
    upd = {"ai_offset": pos, "ai_pages": (probe.get("ai_pages") or 0) + calls}
    if finished:
        upd["ai_done_at"] = now()
    db.update("supplier_probe", upd, {"supplier_id": f"eq.{sup['id']}"})
    print(f"     live: {live} parts", flush=True)
    return calls


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def main():
    if not os.environ.get("GEMINI_API_KEY"):
        print("No free Gemini key - nothing to do.")
        return 0
    sc = _scope.load(db)
    probes = db.select("supplier_probe", "supplier_id,site,product_urls,jsonld_price,ai_offset,ai_pages,ai_done_at",
                       {"product_urls": "gt.0", "ai_done_at": "is.null", "order": "ai_pages.asc.nullsfirst,product_urls.asc"},
                       limit=2000)
    probes = [p for p in probes if (p.get("jsonld_price") or 0) < 2]
    if ONLY:
        probes = [p for p in probes if ONLY.lower() in p["site"].lower()]
    sups = {s["id"]: s for s in db.select("suppliers", "id,name", {"active": "eq.true"}, limit=20000)}
    known = {r["url"] for r in db.select("part_listings", "url", limit=500000) if r.get("url")}
    print(f"{len(probes)} suppliers waiting for the AI reader; up to {DAY_PAGES} pages tonight (free only)", flush=True)
    stats = {"kept": 0, "pages": 0, "not relevant": 0, "newer car only": 0, "fails": 0, "stop": False}
    for p in probes:
        budget = DAY_PAGES - stats["pages"]
        if budget <= 0 or stats["stop"] or time.time() > DEADLINE:
            break
        if p["supplier_id"] in sups:
            try:
                read_site(p, sups[p["supplier_id"]], known, sc, budget, stats)
            except Exception as e:
                print(f"  {sups[p['supplier_id']]['name']}: stopped ({str(e)[:80]})", flush=True)
    print(f"\nAI page reader: {stats['pages']} pages read, {stats['kept']} parts kept"
          f"{' (dry run: nothing saved)' if DRY else ''}. Skipped: not relevant {stats['not relevant']}, "
          f"newer car only {stats['newer car only']}.")


if __name__ == "__main__":
    sys.exit(main())
