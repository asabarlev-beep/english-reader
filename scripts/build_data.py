#!/usr/bin/env python3
"""Builds the offline data for the reader app (runs inside GitHub Actions).

1. Downloads the books from Project Gutenberg and cleans them
   -> cache/books/<id>.txt (one paragraph per line) + cache/data/books.json
2. Streams the English Wiktionary extract from kaikki.org and keeps only words
   that appear in the books -> cache/data/dict.json
   {"l": {lemma: [hebrew, definition]}, "f": {form: lemma}}
3. Copies app/ + cache/ into _site/ and stamps the service-worker version.

Results in cache/ are reused between runs (actions/cache), so the heavy
download happens only when this script changes.
"""
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sys
import time
import urllib.request
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "cache")
SITE = os.path.join(ROOT, "_site")
UA = {"User-Agent": "english-reader-build/1.0 (personal offline reader)"}

# (Gutenberg id, level)  level: 1=בינוני 2=מתקדם 3=מומחה
BOOKS = [
    (46, 1), (11, 1), (12, 1), (16, 1), (55, 1), (113, 1), (271, 1), (236, 1),
    (215, 1), (35, 1), (74, 1), (289, 1),
    (120, 2), (43, 2), (1661, 2), (2852, 2), (36, 2), (5230, 2), (910, 2),
    (45, 2), (514, 2), (103, 2), (64317, 2), (5200, 2), (844, 2), (174, 2),
    (1342, 3), (84, 3), (345, 3), (1260, 3), (768, 3), (1400, 3), (98, 3),
    (2701, 3),
]

KAIKKI_URLS = [
    "https://kaikki.org/dictionary/English/kaikki.org-dictionary-English.jsonl",
    "https://kaikki.org/dictionary/English/kaikki.org-dictionary-English.jsonl.gz",
]

WORD_RE = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)*")
SKIP_POS = {"name", "character", "symbol", "punct", "prefix", "suffix",
            "infix", "interfix", "affix", "circumfix", "phrase", "proverb"}
POS_SHORT = {"noun": "n.", "verb": "v.", "adj": "adj.", "adv": "adv.",
             "pron": "pron.", "prep": "prep.", "conj": "conj.", "intj": "interj.",
             "det": "det.", "num": "num.", "article": "art.", "particle": "part.",
             "contraction": "contr."}


def log(*a):
    print(*a, flush=True)


def fetch(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except Exception as e:  # noqa
            log(f"  fail {url}: {e}")
            if tries > 1:
                time.sleep(3 * (i + 1))
    return None


def book_urls(gid):
    """Gutenberg itself often refuses cloud servers, so try its mirrors too."""
    d = str(gid)
    sub = "0/" + d if len(d) == 1 else "/".join(d[:-1]) + "/" + d
    mirrors = ["https://gutenberg.pglaf.org", "https://aleph.pglaf.org",
               "https://mirrors.xmission.com/gutenberg", "https://gutenberg.readingroo.ms",
               "https://mirror.csclub.uwaterloo.ca/gutenberg"]
    urls = [f"https://www.gutenberg.org/cache/epub/{gid}/pg{gid}.txt"]
    for m in mirrors:
        urls += [f"{m}/cache/epub/{gid}/pg{gid}.txt", f"{m}/{sub}/{gid}-0.txt", f"{m}/{sub}/{gid}.txt"]
    urls += [f"https://www.gutenberg.org/files/{gid}/{gid}-0.txt", f"https://www.gutenberg.org/files/{gid}/{gid}.txt"]
    return urls


GOOD_SOURCES = []


def fetch_book(gid):
    urls = book_urls(gid)
    # try the mirror that worked last time first
    urls.sort(key=lambda u: 0 if any(u.startswith(g) for g in GOOD_SOURCES) else 1)
    for u in urls:
        raw = fetch(u, tries=1)
        if raw and len(raw) > 2000 and b"<html" not in raw[:500].lower():
            base = u.split("/cache/")[0] if "/cache/" in u else u.rsplit("/", 1)[0].rsplit("/" + str(gid), 1)[0]
            base = re.sub(r"(/\d)+$", "", base)
            if base not in GOOD_SOURCES:
                GOOD_SOURCES.insert(0, base)
            log(f"  {gid} <- {u}")
            return raw
    return None


NUM_WORDS = ("One|Two|Three|Four|Five|Six|Seven|Eight|Nine|Ten|Eleven|Twelve|Thirteen|Fourteen|Fifteen|"
             "Sixteen|Seventeen|Eighteen|Nineteen|Twenty|First|Second|Third|Fourth|Fifth|Sixth|Seventh|"
             "Eighth|Ninth|Tenth|Last|the Last|the First")
HEAD_RE = re.compile(
    r"^(?:(?:CHAPTER|Chapter|STAVE|Stave|BOOK|Book|PART|Part|LETTER|Letter|ACT|Act|VOLUME|Volume)\s+"
    r"(?:[IVXLCDM]+|\d+|" + NUM_WORDS + r"|" + NUM_WORDS.upper() + r")\b(?:[.:\-—]?\s*.*)?"
    r"|(?:FIRST|SECOND|THIRD|FOURTH|FIFTH) ACT\b.*"
    r"|[IVXLC]{1,7}(?:\.\s*[A-Z0-9 ,'’\-—.:;!?]*)?)$")


def is_heading(t):
    return len(t) <= 90 and bool(HEAD_RE.match(t))


def clean_book(raw):
    """Returns title, author and lines in the app's format:
    '#\t<text>' chapter heading, '~\t<text>' short line (contents, poems, letters), else a paragraph."""
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    head = text[:6000]
    title = re.search(r"^Title:\s*(.+)$", head, re.M)
    author = re.search(r"^Author:\s*(.+)$", head, re.M)
    s = re.search(r"\*\*\*\s*START OF (?:THE|THIS) PROJECT GUTENBERG.*?\*\*\*", text, re.I | re.S)
    e = re.search(r"\*\*\*\s*END OF (?:THE|THIS) PROJECT GUTENBERG", text, re.I)
    body = text[s.end() if s else 0: e.start() if e else len(text)]
    body = re.sub(r"\[Illustration[^\]]*\]", "", body, flags=re.S)
    body = body.replace("_", "")
    items = []  # (kind, text)  kind: p=paragraph, h=heading candidate, v=short line
    for block in re.split(r"\n\s*\n", body):
        lines = [re.sub(r"\s+", " ", l).strip() for l in block.split("\n")]
        lines = [l for l in lines if l and not re.fullmatch(r"[*\s.]+", l)]
        if not lines:
            continue
        heads = sum(1 for l in lines if is_heading(l))
        lens = [len(l) for l in lines]
        if len(lines) >= 2 and heads >= 2:                       # table of contents
            items += [("v", l) for l in lines]
        elif len(lines) >= 3 and sum(lens) / len(lens) < 48 and max(lens) < 70:   # poem / list
            items += [("v", l) for l in lines]
        elif len(lines) == 2 and is_heading(lines[0]) and len(lines[1]) <= 70:  # "Chapter I" / "The Cyclone"
            items.append(("h", lines[0].rstrip(".") + " — " + lines[1]))
        else:
            t = " ".join(lines)
            if is_heading(t):
                items.append(("h", t))
            elif len(t) < 60 and not re.search(r"[.!?\"”’,;:)\]]$", t):     # titles, headings
                items.append(("v", t))
            else:
                items.append(("p", t))
    out = []
    i = 0
    while i < len(items):
        kind, t = items[i]
        if kind == "h":
            # a real chapter has prose soon after it; otherwise it's a contents entry
            real = any(k == "p" and len(x) > 200 for k, x in items[i + 1:i + 5])
            if real:
                if i + 1 < len(items):
                    k2, t2 = items[i + 1]
                    if k2 != "h" and len(t2) <= 70 and (t2.isupper() or not re.search(r"[.!?\"”]$", t2)):
                        t = t.rstrip(".") + " — " + t2
                        i += 1
                out.append("#\t" + t)
            else:
                out.append("~\t" + t)
        elif kind == "v":
            out.append("~\t" + t)
        else:
            out.append(t)
        i += 1
    return (title.group(1).strip() if title else None,
            author.group(1).strip() if author else None,
            out)


def build_books():
    bdir = os.path.join(CACHE, "books")
    rdir = os.path.join(CACHE, "raw")
    os.makedirs(bdir, exist_ok=True)
    os.makedirs(rdir, exist_ok=True)
    meta = []
    for gid, level in BOOKS:
        rp = os.path.join(rdir, f"{gid}.txt")
        if os.path.exists(rp):
            raw = open(rp, "rb").read()
        else:
            raw = fetch_book(gid)
            if not raw:
                log(f"!! failed book {gid}")
                continue
            open(rp, "wb").write(raw)
            time.sleep(1)
        title, author, lines = clean_book(raw)
        words = sum(len(WORD_RE.findall(p)) for p in lines)
        chapters = sum(1 for l in lines if l.startswith("#\t"))
        with open(os.path.join(bdir, f"{gid}.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        h = hashlib.md5("\n".join(lines).encode("utf-8")).hexdigest()[:8]   # new text -> new address, so phones never keep an old copy
        meta.append({"id": gid, "title": title or f"Book {gid}", "author": author or "",
                     "level": level, "words": words, "chapters": chapters, "file": f"books/{gid}.txt?v={h}"})
        log(f"book {gid}: {title} / {author} ({words} words, {chapters} chapters)")
    os.makedirs(os.path.join(CACHE, "data"), exist_ok=True)
    json.dump(meta, open(os.path.join(CACHE, "data", "books.json"), "w", encoding="utf-8"), ensure_ascii=False)
    return meta


def book_vocab():
    vocab = set()
    bdir = os.path.join(CACHE, "books")
    for name in os.listdir(bdir):
        for w in WORD_RE.findall(open(os.path.join(bdir, name), encoding="utf-8").read().replace("#\t", " ").replace("~\t", " ")):
            w = w.lower().replace("’", "'")
            vocab.add(w)
            if w.endswith("'s"):
                vocab.add(w[:-2])
    return vocab


def open_stream(url):
    req = urllib.request.Request(url, headers=UA)
    r = urllib.request.urlopen(req, timeout=120)
    if url.endswith(".gz"):
        return io.TextIOWrapper(gzip.GzipFile(fileobj=r), encoding="utf-8")
    return io.TextIOWrapper(r, encoding="utf-8")


def he_translations(entry):
    out = []
    lists = [entry.get("translations") or []]
    for s in entry.get("senses") or []:
        lists.append(s.get("translations") or [])
    for lst in lists:
        for t in lst:
            code = t.get("lang_code") or t.get("code")
            if code == "he" or t.get("lang") == "Hebrew":
                w = (t.get("word") or "").strip()
                if w and w not in out and w != "-":
                    out.append(w)
    return out


def build_dict(vocab):
    path = os.path.join(CACHE, "data", "dict.json")
    if os.path.exists(path):
        log("dict: using cache")
        return
    lemmas = {}                  # word -> {"he": [], "d": []}
    forms = defaultdict(list)    # form -> [lemma]
    n = 0
    stream = None
    for url in KAIKKI_URLS:
        try:
            stream = open_stream(url)
            log("dict source:", url)
            break
        except Exception as e:  # noqa
            log("dict source failed:", url, e)
    if stream is None:
        log("!! no dictionary source, writing empty dict (app will use online fallback)")
        json.dump({"l": {}, "f": {}}, open(path, "w", encoding="utf-8"))
        return
    for line in stream:
        n += 1
        if n % 200000 == 0:
            log(f"  {n} entries, {len(lemmas)} kept")
        try:
            e = json.loads(line)
        except Exception:  # noqa
            continue
        word = e.get("word") or ""
        pos = e.get("pos") or ""
        if " " in word or pos in SKIP_POS or e.get("lang_code", "en") != "en":
            continue
        low = word.lower()
        senses = e.get("senses") or []
        # inflected forms listed on the lemma ("walk" -> walked, walking)
        entry_forms = []
        for f in e.get("forms") or []:
            fw = (f.get("form") or "").lower()
            tags = f.get("tags") or []
            if fw and fw != low and fw in vocab and "table-tags" not in tags and "inflection-template" not in tags:
                entry_forms.append(fw)
        # "walked": form of "walk"
        form_of = []
        real_glosses = []
        for s in senses:
            fo = s.get("form_of") or s.get("alt_of")
            if fo:
                for x in fo:
                    if x.get("word"):
                        form_of.append(x["word"].lower())
                continue
            g = s.get("glosses") or s.get("raw_glosses")
            if g and "no-gloss" not in (s.get("tags") or []):
                real_glosses.append(g[-1])
        if low in vocab:
            for lem in form_of:
                if lem != low and lem not in forms[low]:
                    forms[low].append(lem)
        for fw in entry_forms:
            if low not in forms[fw]:
                forms[fw].append(low)
        if not (low in vocab or entry_forms):
            continue
        he = he_translations(e)
        if not real_glosses and not he:
            continue
        d = lemmas.setdefault(low, {"he": [], "d": []})
        for h in he:
            if h not in d["he"] and len(d["he"]) < 5:
                d["he"].append(h)
        if real_glosses and len(d["d"]) < 3:
            g = re.sub(r"\s+", " ", real_glosses[0]).strip()
            if len(g) > 150:
                g = g[:147].rsplit(" ", 1)[0] + "…"
            d["d"].append(f"({POS_SHORT.get(pos, pos)}) {g}")
    log(f"dict: scanned {n} entries")
    out_l = {}
    for w, d in lemmas.items():
        out_l[w] = [", ".join(d["he"]), " · ".join(d["d"])]
    out_f = {}
    for fw, lems in forms.items():
        if fw not in vocab:
            continue
        good = [x for x in lems if x in out_l]
        if good:
            # prefer a lemma that has Hebrew
            good.sort(key=lambda x: 0 if out_l[x][0] else 1)
            out_f[fw] = good[0]
    # drop lemmas that are neither in the books nor reachable through a form
    reach = set(out_f.values())
    out_l = {w: v for w, v in out_l.items() if w in vocab or w in reach}
    with_he = sum(1 for v in out_l.values() if v[0])
    log(f"dict: {len(out_l)} words ({with_he} with Hebrew), {len(out_f)} forms")
    cov = sum(1 for w in vocab if w in out_l or w in out_f)
    log(f"dict: covers {cov}/{len(vocab)} distinct book words")
    json.dump({"l": out_l, "f": out_f}, open(path, "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"))


def assemble():
    if os.path.exists(SITE):
        shutil.rmtree(SITE)
    shutil.copytree(os.path.join(ROOT, "app"), SITE)
    shutil.copytree(os.path.join(CACHE, "books"), os.path.join(SITE, "books"))
    os.makedirs(os.path.join(SITE, "data"))
    for f in ("books.json", "dict.json"):
        shutil.copy(os.path.join(CACHE, "data", f), os.path.join(SITE, "data", f))
    version = os.environ.get("GITHUB_SHA", str(int(time.time())))[:10]
    sw = os.path.join(SITE, "sw.js")
    s = open(sw, encoding="utf-8").read().replace("__BUILD__", version)
    open(sw, "w", encoding="utf-8").write(s)
    open(os.path.join(SITE, ".nojekyll"), "w").close()
    log("site assembled, version", version)


if __name__ == "__main__":
    step = sys.argv[1] if len(sys.argv) > 1 else "all"
    if step in ("data", "all"):
        os.makedirs(os.path.join(CACHE, "data"), exist_ok=True)
        meta = build_books()
        if not meta:
            sys.exit("no books downloaded")
        build_dict(book_vocab())
    if step in ("site", "all"):
        assemble()
