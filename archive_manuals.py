"""
CPF - Find freely posted factory parts catalogues and manuals (24 Nov 2026)
==========================================================================
Fit system, Step 3 (manuals library). Free, no AI tokens.

Searches the Internet Archive (archive.org, a public library of scanned books and
documents) for parts catalogues, parts lists, interchange manuals and shop manuals for
each of our makes. Each find goes to staging_documents with its link; nothing is
downloaded yet. A find is kept only when its title names the make AND a manual type.

Run:  python archive_manuals.py      (AM_BRANDS="Ford,MG", AM_DRY=1, AM_PER_MAKE=40)
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import scope as _scope
SCOPE = _scope.Scope([])      # replaced with the database lists when the run starts

API = "https://archive.org/advancedsearch.php"
KINDS = [("parts catalogue", r"parts?\s+(catalog|catalogue|book|list|manual|price list)|illustrated parts|master parts"),
         ("interchange manual", r"interchange"),
         ("shop manual", r"shop manual|service manual|workshop manual|repair manual|body manual"),
         ("owner manual", r"owner'?s?\s+(manual|handbook|guide)|instruction book|handbook")]


NOT_VEHICLE = re.compile(r"\b(atari|console|game|printer|computer|laptop|calculator|camera|software|hewlett|"
                         r"tractor|lawn|sewing|typewriter|radio|television|tv|phone|aircraft|airplane|boat|outboard|forklift|tcm|onan|generator|marine|carbon fiber|wholesale)\b", re.I)
VEHICLE = re.compile(r"\b(car|cars|auto|automobile|motor|motors|truck|trucks|pickup|van|sedan|coupe|roadster|"
                     r"motorcycle|motorcycles|scooter|chassis|body|engine|passenger)\b", re.I)


def search(make, rows, parts_only=False):
    if make is None:          # interchange manuals rarely name one make in the title
        q = 'title:(interchange) AND (title:(parts) OR title:(manual)) AND mediatype:(texts)'
    elif parts_only:
        q = f'title:("{make}") AND title:(parts) AND mediatype:(texts)'
    else:
        q = (f'title:("{make}") AND (title:(interchange) OR title:(manual) OR title:(handbook)) '
             f'AND mediatype:(texts)')
    params = [("q", q), ("rows", str(rows)), ("output", "json")] + [("fl[]", f) for f in
              ("identifier", "title", "year", "date", "licenseurl", "rights", "imagecount")]
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "CollectorPartsFinder/1.0 (manuals library)"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())["response"]["docs"]


def main():
    from supabase_client import db
    global SCOPE
    SCOPE = _scope.load(db)
    only = [b.strip() for b in os.environ.get("AM_BRANDS", "").split(",") if b.strip()]
    per = int(os.environ.get("AM_PER_MAKE", "40"))
    dry = bool(os.environ.get("AM_DRY"))
    brands = [b for b in db.select("brands", "id,name,vehicle_type,country_of_origin", limit=5000)
              if b.get("vehicle_type") != "corporate" and not b["name"].endswith(" Truck")
              and (not only or b["name"] in only)]
    model_names = {}
    for m in db.select("make_model_menu", "brand_id,name", limit=50000):
        n = re.sub(r"\s*\([^)]*\)\s*$", "", m["name"]).strip()
        if len(n) >= 3:
            model_names.setdefault(m["brand_id"], set()).add(n.lower())
    seen = {r["source_url"] for r in db.select("staging_documents", "source_url", limit=50000) if r.get("source_url")}
    total, by_kind = 0, {}
    for b in sorted(brands, key=lambda x: x["name"]):
        try:
            docs = search(b["name"], per, parts_only=True) + search(b["name"], per)
        except Exception as e:
            print(f"  {b['name']}: search failed {str(e)[:80]}")
            continue
        time.sleep(1)
        rows = []
        for d in docs:
            title = d.get("title") if isinstance(d.get("title"), str) else " ".join(d.get("title") or [])
            if not title or not re.search(r"\b" + re.escape(b["name"]) + r"\b", title, re.I):
                continue
            kind = next((k for k, rx in KINDS if re.search(rx, title, re.I)), None)
            if not kind or NOT_VEHICLE.search(title):
                continue
            low = title.lower()
            if not VEHICLE.search(title) and not any(re.search(r"\b" + re.escape(n) + r"\b", low)
                                                     for n in model_names.get(b["id"], ())):
                continue                       # "Jaguar" alone could be anything; needs a vehicle word or model name
            url = "https://archive.org/details/" + d["identifier"]
            if url in seen:
                continue
            seen.add(url)
            years = [int(y) for y in re.findall(r"\b(19[0-9]{2}|18[89][0-9]|20[0-2][0-9])\b", title)]
            cutoff = SCOPE.cutoff(b, title)
            if years and min(years) > cutoff:
                continue                       # after the collector cut-off
            rows.append({"title": title[:300], "entry_type": "document", "document_type": kind,
                         "brand_text": b["name"], "resolved_brand_id": b["id"],
                         "year_start": min(years) if years else None, "year_end": max(years) if years else None,
                         "source_url": url, "source_kind": "archive", "holder": "Internet Archive",
                         "file_urls": [f"https://archive.org/download/{d['identifier']}"],
                         "summary": f"Found on the Internet Archive; {d.get('imagecount') or '?'} pages; "
                                    f"rights: {d.get('rights') or d.get('licenseurl') or 'not stated'}",
                         "keywords": [b["name"], kind], "validation_status": "pending", "confidence": 0.6})
            by_kind[kind] = by_kind.get(kind, 0) + 1
        if rows:
            print(f"  {b['name']:24} {len(rows)} manuals found")
            for r in rows[:3]:
                print(f"      {r['document_type']:20} {r['title'][:90]}")
            if not dry:
                db.insert("staging_documents", rows)
        total += len(rows)
    print(f"\n{total} manuals found: {by_kind}")


if __name__ == "__main__":
    main()
    sys.exit(0)
