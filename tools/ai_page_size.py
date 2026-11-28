"""Measure how much text an AI reader would have to read per product page (cost estimate only; no AI called)."""
import random, re, html, sys, urllib.parse, statistics
sys.path.insert(0, ".")
from supabase_client import db
import site_probe as sp

rows = db.select("supplier_probe", "site,product_urls,jsonld_price", {"product_urls": "gt.0"}, limit=2000)
rows = [r for r in rows if (r.get("jsonld_price") or 0) < 2]
random.seed(7); random.shuffle(rows)
sizes = []
for r in rows[:15]:
    site = r["site"] if r["site"].startswith("http") else "https://" + r["site"]
    try:
        final, home = sp.get(site, 600_000)
        base = "{0.scheme}://{0.netloc}".format(urllib.parse.urlparse(final))
        prods = [u for u in sp.sitemap_urls(base, home) if sp.PRODUCT_URL.search(urllib.parse.urlparse(u).path)]
    except Exception as e:
        print("skip", site, str(e)[:50]); continue
    for u in random.sample(prods, min(3, len(prods))):
        try:
            _, page = sp.get(u, 1_500_000)
        except Exception:
            continue
        t = re.sub(r"(?is)<(script|style|noscript|svg|header|footer|nav)[^>]*>.*?</\1>", " ", page)
        t = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t))).strip()
        sizes.append(len(t))
        print(f"{site[:40]:40} {len(t):7} chars  {u[:70]}")
if sizes:
    print(f"\nPAGES {len(sizes)}  median {statistics.median(sizes):.0f} chars  mean {statistics.mean(sizes):.0f}  ~tokens median {statistics.median(sizes)/4:.0f}")
