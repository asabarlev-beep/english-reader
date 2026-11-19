#!/usr/bin/env python3
"""Fills in Hebrew for dictionary words that Wiktionary has no Hebrew translation for,
using an offline machine-translation model (Helsinki-NLP opus-mt-en-he) on the build server.
Machine translations are marked (third field = 1) so the app can label them.
"""
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "cache", "data", "dict.json")
MEMO = os.path.join(ROOT, "cache", "data", "mt_memo.json")   # word -> Hebrew, kept across rebuilds
MODEL = "Helsinki-NLP/opus-mt-en-he"
HEB = re.compile(r"[֐-׿]")


def main():
    d = json.load(open(PATH, encoding="utf-8"))
    if d.get("mt"):
        print("mt: already done")
        return
    memo = json.load(open(MEMO, encoding="utf-8")) if os.path.exists(MEMO) else {}
    reused = 0
    for w, v in d["l"].items():
        if not v[0] and memo.get(w):
            d["l"][w] = [memo[w], v[1], 1]
            reused += 1
    todo = [w for w, v in d["l"].items() if not v[0] and w.isalpha() and len(w) > 1 and w not in memo]
    print(f"mt: reused {reused}, {len(todo)} new words without Hebrew")
    if not todo:
        d["mt"] = True
        json.dump(d, open(PATH, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
        return
    import torch
    from transformers import MarianMTModel, MarianTokenizer
    torch.set_num_threads(os.cpu_count() or 2)
    tok = MarianTokenizer.from_pretrained(MODEL)
    model = MarianMTModel.from_pretrained(MODEL).eval()
    t0, done = time.time(), 0
    B = 64
    for i in range(0, len(todo), B):
        batch = todo[i:i + B]
        enc = tok(batch, return_tensors="pt", padding=True)
        with torch.no_grad():
            out = model.generate(**enc, num_beams=1, max_new_tokens=12)
        for w, o in zip(batch, tok.batch_decode(out, skip_special_tokens=True)):
            h = re.sub(r"[.!?,;:\"'״׳]+$", "", o.strip()).strip()
            # keep only short, real Hebrew answers
            if h and HEB.search(h) and len(h.split()) <= 3 and len(h) <= 24:
                v = d["l"][w]
                d["l"][w] = [h, v[1], 1]
                done += 1
            memo[w] = d["l"][w][0] if d["l"][w][2:] == [1] else ""
        if i % (B * 40) == 0:
            print(f"  {i}/{len(todo)}  {time.time() - t0:.0f}s", flush=True)
    d["mt"] = True
    json.dump(d, open(PATH, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    json.dump(memo, open(MEMO, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"mt: added Hebrew to {done} words in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never break the deploy because of this step
        print("mt failed:", e, file=sys.stderr)
