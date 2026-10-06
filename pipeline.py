#!/usr/bin/env python3
"""Gangnam Beauty Guide review pipeline (Python 3.9, stdlib only).

Steps: ingest -> extract -> normalise -> dedupe -> validate -> publish
Each step writes out/<step>.json; `run --from <step>` resumes from saved outputs.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import unicodedata

ROOT = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(ROOT, "fixtures")
OUT = os.path.join(ROOT, "out")
CACHE = os.path.join(OUT, "cache")
STEPS = ["ingest", "extract", "normalise", "dedupe", "validate", "publish"]
PROCEDURES = ["rhinoplasty", "double_eyelid", "facial_contouring", "laser", "skincare", "other"]
# Plausible total price ranges in KRW per procedure (deterministic sanity check).
PRICE_RANGE = {
    "rhinoplasty": (1_500_000, 15_000_000),
    "double_eyelid": (500_000, 5_000_000),
    "facial_contouring": (4_000_000, 30_000_000),
    "laser": (30_000, 5_000_000),
    "skincare": (30_000, 3_000_000),
    "other": (10_000, 50_000_000),
}
SPONSORED_RE = re.compile(r"체험단|원고료|수수료|협찬")
LLM_TIMEOUT = 180
STATS = {"llm_calls": 0, "llm_cache_hits": 0}


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save(name, data):
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


# ---------------------------------------------------------------- LLM helper
def parse_json_loose(text):
    """Strip ``` fences / prose and decode the first JSON array or object."""
    t = re.sub(r"```(?:json)?", "", text).strip()
    starts = [i for i in (t.find("["), t.find("{")) if i != -1]
    if not starts:
        raise ValueError("no JSON found in LLM output: %r" % text[:200])
    obj, _ = json.JSONDecoder().raw_decode(t[min(starts):])
    return obj


def _find_key(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        obj = list(obj.values())
    if isinstance(obj, list):
        for v in obj:
            found = _find_key(v, key)
            if found is not None:
                return found
    return None


def openclaw_call(prompt, sha):
    pfile = os.path.join(CACHE, "%s.prompt.txt" % sha)
    with open(pfile, "w", encoding="utf-8") as f:
        f.write(prompt)
    proc = subprocess.run(
        ["openclaw", "agent", "--session-id", "gbg-%s-%d" % (sha, int(time.time())),
         "--message-file", pfile, "--json"],
        capture_output=True, text=True, timeout=LLM_TIMEOUT, cwd=ROOT,
    )
    os.remove(pfile)
    if proc.returncode != 0:
        raise RuntimeError("openclaw agent exited %d: %s" % (proc.returncode, proc.stderr[-500:]))
    out = proc.stdout
    envelope = json.JSONDecoder().raw_decode(out[out.find("{"):])[0]
    reply = _find_key(envelope, "terminalReply")
    if not reply or not reply.get("text"):
        raise RuntimeError("openclaw agent returned no reply text: %s" % out[-500:])
    return reply["text"]


def llm_json(prompt, label):
    """One headless Claude CLI call, cached by prompt sha256."""
    os.makedirs(CACHE, exist_ok=True)
    sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
    cpath = os.path.join(CACHE, "%s.json" % sha)
    if os.path.exists(cpath):
        STATS["llm_cache_hits"] += 1
        log("%s: cache hit %s" % (label, sha))
        return load(cpath)["parsed"]
    log("%s: calling claude -p (prompt %d chars)" % (label, len(prompt)))
    t0 = time.time()
    proc = subprocess.run(
        ["claude", "-p", "--model", "sonnet", "--output-format", "text"],
        input=prompt, capture_output=True, text=True, timeout=LLM_TIMEOUT, cwd=ROOT,
    )
    STATS["llm_calls"] += 1
    backend = "claude-cli"
    if proc.returncode == 0:
        raw = proc.stdout
    else:
        # Fallback: the local claude CLI's OAuth session can expire (see NOTES.md).
        # The OpenClaw gateway holds its own Anthropic auth, so route the same prompt there.
        err = (proc.stderr + proc.stdout).strip().splitlines()
        log("%s: claude CLI failed (%s); falling back to openclaw agent" % (label, err[-1] if err else proc.returncode))
        raw = openclaw_call(prompt, sha)
        backend = "openclaw-gateway"
    parsed = parse_json_loose(raw)
    with open(cpath, "w", encoding="utf-8") as f:
        json.dump({"label": label, "backend": backend, "prompt": prompt, "raw": raw, "parsed": parsed,
                   "seconds": round(time.time() - t0, 1)}, f, ensure_ascii=False, indent=2)
    log("%s: done in %.1fs" % (label, time.time() - t0))
    return parsed


# ---------------------------------------------------------------- steps
def step_ingest(_):
    posts = load(os.path.join(FIX, "raw_posts.json"))
    log("ingest: %d raw posts" % len(posts))
    return posts


def step_extract(posts):
    items = [{"id": p["id"], "text": p["text"]} for p in posts]
    prompt = (
        "You extract structured data from Korean plastic-surgery / aesthetic-clinic reviews.\n"
        "Return ONLY a JSON array, no prose, no code fences. One object per input post, same ids:\n"
        '{"id": str, "translation_en": str (faithful English translation, keep all numbers, '
        'write prices as KRW e.g. "3,500,000 KRW (350만원)"), '
        '"procedure": one of %s, "clinic_name_raw": str exactly as written in Korean/English in the post or null, '
        '"surgeon_name_raw": str exactly as written (Korean name, without 원장님) or null, '
        '"price_krw": int total price paid in KRW (만원 = x10000, so 350만원 = 3500000; if per-session and total both given use the total) or null, '
        '"sentiment": int 1-5, "sponsored_signals": list of the exact Korean phrases that disclose sponsorship '
        '(e.g. 체험단, 원고료를 지원받아, 소정의 수수료, 협찬), [] if none}\n\n'
        "Posts:\n%s" % (PROCEDURES, json.dumps(items, ensure_ascii=False, indent=1))
    )
    result = llm_json(prompt, "extract")
    by_id = {r["id"]: r for r in result}
    out = []
    for p in posts:
        ex = by_id.get(p["id"])
        if ex is None:
            ex = {"error": "missing from LLM output"}
        if ex.get("procedure") not in PROCEDURES:
            ex["procedure_raw"], ex["procedure"] = ex.get("procedure"), "other"
        # Defensive coercion: LLMs sometimes return "3,500,000" or 350 for 350만원.
        pr = ex.get("price_krw")
        if isinstance(pr, str):
            digits = re.sub(r"[^0-9]", "", pr)
            pr = int(digits) if digits else None
        ex["price_krw"] = pr
        out.append(dict(p, extracted=ex))
    log("extract: %d posts extracted" % len(out))
    return out


def norm_clinic(s):
    s = unicodedata.normalize("NFKC", s or "").lower()
    for suffix in ["성형외과", "피부과", "의원", "plastic surgery", "clinic", "ps", "dermatology", "derm"]:
        s = s.replace(suffix, "")
    return re.sub(r"[\s\W_]+", "", s)


def step_normalise(posts):
    clinics = load(os.path.join(FIX, "clinics.json"))
    index = {}
    for c in clinics:
        for name in [c["name_ko"], c["name_en"]] + c["aliases"]:
            index[norm_clinic(name)] = c["id"]
    unresolved = []
    for p in posts:
        raw = p["extracted"].get("clinic_name_raw")
        key = norm_clinic(raw)
        if key and key in index:
            p["clinic_id"], p["clinic_match"] = index[key], "deterministic"
        else:
            p["clinic_id"], p["clinic_match"] = None, "unresolved"
            if raw:
                unresolved.append(raw)
    if unresolved:
        registry = [{"id": c["id"], "name_ko": c["name_ko"], "name_en": c["name_en"],
                     "aliases": c["aliases"]} for c in clinics]
        prompt = (
            "Map each raw clinic name to a clinic id from this registry, or \"UNKNOWN\" if it is not "
            "clearly the same clinic. Never invent ids. Return ONLY a JSON object {raw_name: id_or_UNKNOWN}.\n"
            "Registry:\n%s\nRaw names:\n%s"
            % (json.dumps(registry, ensure_ascii=False), json.dumps(sorted(set(unresolved)), ensure_ascii=False))
        )
        answer = llm_json(prompt, "clinic-tiebreak")
        valid = {c["id"] for c in clinics}
        for p in posts:
            raw = p["extracted"].get("clinic_name_raw")
            if p["clinic_id"] is None and raw:
                cid = answer.get(raw, "UNKNOWN")
                if cid in valid:  # constrain: only existing ids accepted
                    p["clinic_id"], p["clinic_match"] = cid, "llm"
                else:
                    p["clinic_match"] = "llm_unknown"
    counts = {}
    for p in posts:
        counts[p["clinic_match"]] = counts.get(p["clinic_match"], 0) + 1
    log("normalise: %s" % counts)
    return posts


EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")


def fingerprint_text(t):
    t = unicodedata.normalize("NFKC", t)
    t = EMOJI_RE.sub("", t)
    t = re.sub(r"[^\w]", "", t)  # drop whitespace and punctuation
    return t


def trigrams(t):
    return {t[i:i + 3] for i in range(max(len(t) - 2, 1))}


def jaccard(a, b):
    return len(a & b) / float(len(a | b) or 1)


def step_dedupe(posts):
    reviews = []
    for p in posts:
        grams = trigrams(fingerprint_text(p["text"]))  # ORIGINAL Korean, never the translation
        match = None
        for r in reviews:
            sim = jaccard(grams, r["_grams"])
            if sim >= 0.8:
                match = (r, sim)
                break
        src = {"post_id": p["id"], "source": p["source"], "url": p["url"], "posted_at": p["posted_at"]}
        if match:
            r, sim = match
            src["similarity"] = round(sim, 3)
            r["sources"].append(src)
            # keep union of sponsored signals across copies
            r["extracted"]["sponsored_signals"] = sorted(set(r["extracted"].get("sponsored_signals") or [])
                                                         | set(p["extracted"].get("sponsored_signals") or []))
        else:
            reviews.append({"review_id": "r%d" % (len(reviews) + 1), "text_ko": p["text"],
                            "extracted": p["extracted"], "clinic_id": p["clinic_id"],
                            "clinic_match": p["clinic_match"], "sources": [src], "_grams": grams})
    for r in reviews:
        del r["_grams"]
    log("dedupe: %d posts -> %d reviews (%d merged)" % (len(posts), len(reviews), len(posts) - len(reviews)))
    return reviews


def step_validate(reviews):
    items = []
    for r in reviews:
        ex = r["extracted"]
        items.append({"id": r["review_id"], "korean": r["text_ko"], "english": ex.get("translation_en"),
                      "fields": {k: ex.get(k) for k in ["procedure", "clinic_name_raw", "surgeon_name_raw",
                                                       "price_krw", "sentiment", "sponsored_signals"]}})
    prompt = (
        "You are a strict QA critic for translated Korean medical-clinic reviews. For each item compare the "
        "Korean original with the English translation and extracted fields. Check: translation faithfulness, "
        "especially numbers (price: 만원 = 10,000 KRW, so price_krw must equal the total in the Korean), dates, "
        "surgeon name; procedure correct; whether the post is sponsored (체험단 / 원고료 / 소정의 수수료 / 협찬 "
        "disclosures). Return ONLY a JSON array, no prose: "
        '[{"id": str, "pass": bool, "sponsored": bool, "reason": str (short, concrete)}]\n\n'
        "Items:\n%s" % json.dumps(items, ensure_ascii=False, indent=1)
    )
    verdicts = {v["id"]: v for v in llm_json(prompt, "critic")}
    for r in reviews:
        ex = r["extracted"]
        v = verdicts.get(r["review_id"], {"pass": False, "sponsored": None, "reason": "critic gave no verdict"})
        reasons = []
        if not v.get("pass"):
            reasons.append("critic: " + v.get("reason", ""))
        # deterministic: plausible price range
        price, proc = ex.get("price_krw"), ex.get("procedure", "other")
        lo, hi = PRICE_RANGE.get(proc, PRICE_RANGE["other"])
        price_ok = price is None or (isinstance(price, int) and lo <= price <= hi)
        if not price_ok:
            reasons.append("price %s outside plausible range %d-%d for %s" % (price, lo, hi, proc))
        # deterministic: sponsored regex on Korean original overrides the LLMs
        hits = sorted(set(SPONSORED_RE.findall(r["text_ko"])))
        sponsored = bool(hits)
        overrides = []
        if bool(v.get("sponsored")) != sponsored:
            overrides.append("critic said sponsored=%s, regex says %s" % (v.get("sponsored"), sponsored))
        if bool(ex.get("sponsored_signals")) != sponsored:
            overrides.append("extractor signals=%s, regex says %s" % (ex.get("sponsored_signals"), sponsored))
        if sponsored:
            reasons.append("sponsored disclosure: %s" % ", ".join(hits))
        if r["clinic_id"] is None:
            reasons.append("clinic not in registry: %s" % ex.get("clinic_name_raw"))
        r["validation"] = {"critic_pass": bool(v.get("pass")), "critic_reason": v.get("reason"),
                           "critic_sponsored": v.get("sponsored"), "price_ok": price_ok,
                           "sponsored_regex_hits": hits, "sponsored": sponsored,
                           "regex_overrides": overrides, "status": "quarantine" if reasons else "publish",
                           "reasons": reasons}
    n_ok = sum(1 for r in reviews if r["validation"]["status"] == "publish")
    log("validate: %d publish, %d quarantine" % (n_ok, len(reviews) - n_ok))
    return reviews


def step_publish(reviews):
    clinics = {c["id"]: c for c in load(os.path.join(FIX, "clinics.json"))}
    published, quarantine = [], []
    for r in reviews:
        ex, c = r["extracted"], clinics.get(r["clinic_id"])
        badges = []
        if c and ex.get("surgeon_name_raw") in c.get("surgeons", []):
            badges.append("verified_surgeon")
        if c and ex.get("procedure") in c.get("procedures", []):
            badges.append("verified_procedure")
        if len(r["sources"]) > 1:
            badges.append("multi_source")
        rec = {"review_id": r["review_id"], "clinic_id": r["clinic_id"],
               "clinic_name_en": c["name_en"] if c else None, "procedure": ex.get("procedure"),
               "surgeon": ex.get("surgeon_name_raw"), "price_krw": ex.get("price_krw"),
               "sentiment": ex.get("sentiment"), "translation_en": ex.get("translation_en"),
               "text_ko": r["text_ko"], "sources": r["sources"], "trust_badges": badges}
        if r["validation"]["status"] == "publish":
            published.append(rec)
        else:
            rec["reasons"] = r["validation"]["reasons"]
            quarantine.append(rec)
    save("published.json", published)
    save("quarantine.json", quarantine)
    lines = ["# Gangnam Beauty Guide: published reviews by clinic", "",
             "| Clinic | Reviews | Avg sentiment | Price range (KRW) | Trust badges |",
             "|---|---|---|---|---|"]
    by_clinic = {}
    for p in published:
        by_clinic.setdefault(p["clinic_id"], []).append(p)
    for cid, rs in sorted(by_clinic.items()):
        prices = [p["price_krw"] for p in rs if p["price_krw"]]
        sents = [p["sentiment"] for p in rs if p["sentiment"]]
        badges = sorted({b for p in rs for b in p["trust_badges"]})
        lines.append("| %s | %d | %s | %s | %s |" % (
            clinics[cid]["name_en"], len(rs), "%.1f" % (sum(sents) / len(sents)) if sents else "-",
            "{:,}-{:,}".format(min(prices), max(prices)) if prices else "-", ", ".join(badges) or "-"))
    lines += ["", "Quarantined: %d review(s), see quarantine.json." % len(quarantine), ""]
    for q in quarantine:
        lines.append("- %s (%s): %s" % (q["review_id"], q["clinic_id"] or "unknown clinic", "; ".join(q["reasons"])))
    with open(os.path.join(OUT, "index.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    summary = {"reviews_in": len(reviews), "published": len(published), "quarantined": len(quarantine)}
    log("publish: %s" % summary)
    return summary


FUNCS = {"ingest": step_ingest, "extract": step_extract, "normalise": step_normalise,
         "dedupe": step_dedupe, "validate": step_validate, "publish": step_publish}


def run(start):
    i = STEPS.index(start)
    data = None if i == 0 else load(os.path.join(OUT, "%s.json" % STEPS[i - 1]))
    t0 = time.time()
    for name in STEPS[i:]:
        data = FUNCS[name](data)
        save("%s.json" % name, data)
    log("done in %.1fs, llm calls=%d, cache hits=%d" % (time.time() - t0, STATS["llm_calls"], STATS["llm_cache_hits"]))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run")
    r.add_argument("--from", dest="start", default="ingest", choices=STEPS)
    args = ap.parse_args()
    if args.cmd != "run":
        ap.print_help()
        sys.exit(1)
    run(args.start)


if __name__ == "__main__":
    main()
