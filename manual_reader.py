"""
CPF - Read part lines out of freely posted manuals (24 Nov 2026)
================================================================
Fit system, Step 3. Takes manuals the finder put in staging_documents (Internet Archive),
downloads the text the Archive already made from the scans (free, no OCR cost), and reads
the part-number lines with the free Gemini model (Haiku as backup).

Every line is checked against the page before it is saved: the part number and at least
one word of its description must appear on that page. Lines that fail are dropped.
The manual is then added to the Knowledge Library (documents) with its source and page
count, and its lines go to manual_lines with page numbers.

Parts catalogues and interchange manuals first; shop and owner manuals later.
Run:  python manual_reader.py      (MR_LIMIT=5 manuals, MR_PAGES=40 pages each, MR_DRY=1)
"""
import json
import os
import re
import sys
import urllib.request

import gemini_client
from anthropic_client import claude
from config import MODEL_CLASSIFIER

SYSTEM = """You read one page of a vehicle parts catalogue or interchange manual (scanned, so the text can be messy).
List every part line you can read: the part number, its description, and what it fits as printed (models, years, notes).
Copy part numbers EXACTLY as printed. Do not invent or complete anything. Skip lines without a part number.
Return JSON only: {"lines":[{"part_number":"...","description":"...","fits":"...","year_start":null,"year_end":null}]}"""
DOC_TYPE = {"parts catalogue": "parts_catalog", "interchange manual": "interchange",
            "shop manual": "workshop_manual", "owner manual": "owners_manual"}
NUMBERISH = re.compile(r"\b(?=[A-Z0-9.\- ]{4,20}\b)(?=[^ ]*\d)[A-Z0-9]{1,5}[-. ]?[A-Z0-9]{2,7}(?:[-. ][A-Z0-9]{1,4}){0,2}\b")


def get(url, cap=8_000_000, tries=4):
    import time
    req = urllib.request.Request(url, headers={"User-Agent": "CollectorPartsFinder/1.0 (manuals library)"})
    for n in range(tries):          # the Archive sometimes drops the connection; try again
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read(cap)
        except Exception:
            if n == tries - 1:
                raise
            time.sleep(5 * (n + 1))


def archive_text(identifier):
    import urllib.parse
    meta = json.loads(get(f"https://archive.org/metadata/{urllib.parse.quote(identifier)}"))
    files = [f["name"] for f in meta.get("files", []) if f["name"].endswith("_djvu.txt")]
    if not files:
        return None, meta
    import urllib.parse
    return get(f"https://archive.org/download/{identifier}/{urllib.parse.quote(files[0])}").decode("utf-8", "replace"), meta


STATS_P = {"pattern": 0, "ai": 0}
HAIKU_CAP = float(os.environ.get("MR_HAIKU_USD", "1.00"))   # most Claude may spend in one run
GEMINI_OUT = {"hit": False}                                 # set once Google's free daily allowance is used up


class BudgetStop(Exception):
    """Free Gemini allowance used up and the Claude cap reached: stop, finish tomorrow."""


def read_page(text):
    """Pages are read in pieces of about 6,000 characters: fewer calls, answers never cut off."""
    out, by = [], ""
    for i in range(0, min(len(text), 12000), 6000):
        lines, by = read_piece(text[i:i + 6100])
        out += lines
    return out, by


def read_piece(text):
    user = "Page text:\n\n" + text
    raw, by = None, "archive text + gemini"
    if gemini_client.enabled() and not GEMINI_OUT["hit"]:
        import time
        for attempt in range(3):          # a per-minute limit clears in seconds; the daily one does not
            try:
                raw = gemini_client.message(SYSTEM, user, max_tokens=8000)
                break
            except Exception as e:
                if not re.search(r"429|RESOURCE_EXHAUSTED|quota", str(e), re.I):
                    break
                if attempt == 2:
                    GEMINI_OUT["hit"] = True
                    print("      Google's free daily allowance looks used up for today")
                else:
                    time.sleep(30)
    if not raw:
        if claude.spent_usd >= HAIKU_CAP:
            raise BudgetStop(f"Claude cap ${HAIKU_CAP:.2f} reached")
        raw, by = claude.message(MODEL_CLASSIFIER, SYSTEM, user, max_tokens=8000), "archive text + haiku"
    raw = re.sub(r"^```(json)?|```$", "", raw.strip()).strip()
    data = json.loads(raw)
    lines = data if isinstance(data, list) else data.get("lines", [])
    return [l for l in lines if isinstance(l, dict)], by


# ── Free pattern reader (no AI) ─────────────────────────────────────────────
# Factory parts catalogues usually print one part per line: NUMBER, then a DESCRIPTION in capitals
# ("4573 761   BRACKET, Torque Reaction, Front"). Those lines are read with a pattern for free. A page
# goes to the AI only when the pattern can't account for most of its part numbers.
PN = r"(?:[A-Z]{0,4}\d[A-Z0-9]{1,9}(?:[- .][A-Z0-9]{1,6}){0,3})"
LINE = re.compile(r"^\s*(?:\d{1,3}[.)]?\s+)?(" + PN + r")\s+([A-Z][A-Z0-9&/'\-]*(?:[ ,.;:\-]+[A-Z0-9&/'\-().\"=<>]+){0,20})[ .,]*$")


def pattern_page(page):
    """Return (lines, share of the page's part numbers the pattern explained)."""
    heading = " ".join(l.strip() for l in page.strip().splitlines()[:2] if l.strip())[:120]
    found, seen = [], set()
    for raw in page.splitlines():
        raw = raw.replace("\t", " ")
        m = LINE.match(raw.upper())
        if not m:
            continue
        pn, desc = m.group(1).strip(), raw[m.start(2):m.end(2)].strip()
        if not re.search(r"\d", pn) or len(re.sub(r"[^A-Z0-9]", "", pn)) < 5 or not re.search(r"[A-Za-z]{3}", desc):
            continue
        if re.fullmatch(r"(19|20)\d\d", re.sub(r"[^0-9]", "", pn)):
            continue                                   # a year, not a part number
        key = re.sub(r"[^A-Z0-9]", "", pn)
        if key in seen:
            continue
        seen.add(key)
        found.append({"part_number": pn, "description": desc[:200],
                      "fits": f"Page heading: {heading}" if heading else None})
    # lines that look like part lines (start with a number-like code) are what the pattern must explain
    cand = sum(1 for l in page.splitlines()
               if re.match(r"^\s*(?:\d{1,3}[.)]?\s+)?[A-Z]{0,4}\d[A-Z0-9]{3,}", l.upper())
               and not re.match(r"^\s*(19|20)\d\d\b", l))
    return found, (len(found) / cand if cand else 0)


def squash(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def main():
    from supabase_client import db
    limit, max_pages, dry = int(os.environ.get("MR_LIMIT", "5")), int(os.environ.get("MR_PAGES", "40")), bool(os.environ.get("MR_DRY"))
    staged = db.select("staging_documents", "id,title,document_type,brand_text,resolved_brand_id,year_start,year_end,source_url,summary,validation_status,source_kind",
                       {"validation_status": "eq.pending", "source_kind": "eq.archive"}, limit=50000)
    order = {"parts catalogue": 0, "interchange manual": 1, "shop manual": 2, "owner manual": 3}
    # one manual per make in turn, so the library covers many makes rather than one uploader's set
    staged.sort(key=lambda r: (order.get(r.get("document_type"), 9), r.get("brand_text") or ""))
    turn, rank = {}, {}
    for r in staged:
        k = (order.get(r.get("document_type"), 9), r.get("brand_text"))
        turn[k] = turn.get(k, 0) + 1
        rank[r["id"]] = turn[k]
    staged.sort(key=lambda r: (order.get(r.get("document_type"), 9), rank[r["id"]], r.get("brand_text") or ""))
    done = 0
    for s in staged:
        if done >= limit:
            break
        ident = s["source_url"].rsplit("/", 1)[1]
        try:
            text, meta = archive_text(ident)
        except Exception as e:
            print(f"  {s['title'][:60]}: download failed {str(e)[:80]}")
            continue
        if not text:
            print(f"  {s['title'][:60]}: no text version on the Archive; left for OCR later")
            if not dry:
                db.update("staging_documents", {"validation_status": "needs_review",
                                                "validation_errors": ["no text version; needs OCR"]}, {"id": f"eq.{s['id']}"})
            continue
        pages = text.split("\f") if "\f" in text else [text[i:i + 4000] for i in range(0, len(text), 4000)]
        # pages with the most number-like tokens are the parts pages
        scored = sorted(range(len(pages)), key=lambda i: -len(NUMBERISH.findall(pages[i].upper())))[:max_pages]
        kept, dropped, by = [], 0, ""
        todo_pages = [i for i in sorted(scored) if len(NUMBERISH.findall(pages[i].upper())) >= 5]
        if os.environ.get("MR_SHOW_PAGE") and todo_pages:          # show raw text so the pattern can be tuned
            for i in todo_pages[:2]:
                print(f"----- raw page {i + 1} (first 25 lines) -----")
                for l in pages[i].splitlines()[:25]:
                    print("      |" + l[:110])

        def one(i):
            lines, share = pattern_page(pages[i])
            if len(lines) >= 12 and share >= 0.5:          # the pattern explains this page: no AI needed
                return i, (lines, "archive text + pattern"), None
            if os.environ.get("MR_NO_AI"):
                return i, ([], ""), "left for the AI"
            try:
                return i, read_page(pages[i]), None
            except BudgetStop:
                raise
            except Exception as e:
                return i, ([], ""), str(e)[:60]

        from concurrent.futures import ThreadPoolExecutor
        try:
            with ThreadPoolExecutor(int(os.environ.get("MR_THREADS", "3"))) as ex:   # 3 pages at a time
                results = list(ex.map(one, todo_pages))
        except BudgetStop as e:
            print(f"  STOP: {e}. Unread manuals stay in the queue for the next run.")
            break
        for i, (lines, by_i), err in results:
            page = pages[i]
            if err:
                print(f"      page {i + 1}: could not read ({err})")
                continue
            by = by_i or by
            STATS_P["pattern" if by_i == "archive text + pattern" else "ai"] += 1
            sq = squash(page)
            words = set(re.findall(r"[a-z]{4,}", page.lower()))
            for ln in lines:
                pn, desc = (ln.get("part_number") or "").strip(), (ln.get("description") or "").strip()
                ok = len(squash(pn)) >= 4 and squash(pn) in sq and any(w in words for w in re.findall(r"[a-z]{4,}", desc.lower()))
                if not ok:
                    dropped += 1
                    continue
                kept.append({"page": i + 1, "make": s.get("brand_text"), "part_number": pn[:60], "description": desc[:300],
                             "fits_text": (ln.get("fits") or "")[:300] or None,
                             "year_start": ln.get("year_start") if isinstance(ln.get("year_start"), int) else None,
                             "year_end": ln.get("year_end") if isinstance(ln.get("year_end"), int) else None,
                             "raw_text": None, "read_by": by})
        print(f"  {s['title'][:70]:70} {len(pages)} pages; {len(kept)} part lines kept, {dropped} dropped (not on the page)")
        for k in kept[:5]:
            print(f"      p{k['page']}: {k['part_number']} | {k['description'][:50]} | {(k['fits_text'] or '')[:40]}")
        done += 1
        if dry:
            continue
        doc = db.insert("documents", {"title": s["title"], "entry_type": "manual",
                                      "document_type": DOC_TYPE.get(s["document_type"], "parts_manual"),
                                      "brand_id": s.get("resolved_brand_id"), "year_start": s.get("year_start"),
                                      "year_end": s.get("year_end"), "summary": s.get("summary"),
                                      "keywords": [s.get("brand_text") or "", s["document_type"]],
                                      "reference_links": [s["source_url"]], "verified": False, "active": True,
                                      "source_kind": "archive", "source_url": s["source_url"], "holder": "Internet Archive",
                                      "permission": "public_posting"})
        doc_id = doc[0]["id"]
        db.insert("document_files", {"document_id": doc_id, "file_url": s["source_url"], "file_type": "archive_text",
                                     "page_count": len(pages), "ocr_status": "done"})
        for j in range(0, len(kept), 500):
            db.insert("manual_lines", [{**k, "document_id": doc_id} for k in kept[j:j + 500]])
        db.update("staging_documents", {"validation_status": "passed", "promoted_id": doc_id}, {"id": f"eq.{s['id']}"})
    print(f"\nPattern reader: {STATS_P['pattern']} pages read free, {STATS_P['ai']} pages needed the AI")
    print(f"\n{done} manuals read. Gemini: {gemini_client.summary()}  Claude: ${claude.spent_usd:.2f}")


if __name__ == "__main__":
    main()
    sys.exit(0)
