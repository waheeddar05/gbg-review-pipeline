# gbg-review-pipeline

A stdlib-only Python 3.9 pipeline for Gangnam Beauty Guide. It ingests Korean clinic reviews
scraped from Naver cafe/blog, Daum and clinic sites, then translates and extracts them with an LLM.
It resolves clinic names against a registry, de-duplicates cross-posts on the original Korean text,
and checks each review with a separate LLM critic plus deterministic rules. Only clean, organic
reviews are published, with trust badges.

## Run

```
python3 pipeline.py run                 # all steps
python3 pipeline.py run --from dedupe   # resume from saved out/normalise.json
```

You don't need to pip-install anything. LLM calls go to `claude -p --model sonnet --output-format text`
with the prompt on stdin and a 180 s timeout. If the CLI fails (for example, its login has expired),
the same prompt goes through the local OpenClaw gateway (`openclaw agent --message-file`).
Responses are cached in `out/cache/<sha>.json`, so reruns are instant and deterministic.
The full uncached run took 45 s and made 3 LLM calls.

## Steps

```
fixtures/raw_posts.json
   │ ingest      load 8 Korean posts                                  → out/ingest.json
   ▼
   │ extract     ONE batched LLM call: translation_en, procedure enum,
   │             clinic/surgeon raw names, price_krw (만원×10000),
   │             sentiment, sponsored_signals                         → out/extract.json
   ▼
   │ normalise   deterministic match against fixtures/clinics.json
   │             (NFKC, lowercase, strip 성형외과/피부과/의원/clinic/PS,
   │             drop spaces). Leftovers → ONE LLM tie-break call that
   │             may only answer an existing id or UNKNOWN            → out/normalise.json
   ▼
   │ dedupe      ORIGINAL Korean text, emoji and whitespace stripped,
   │             char 3-gram Jaccard ≥ 0.8 → one review, many sources → out/dedupe.json
   ▼
   │ validate    ONE batched critic LLM call (Korean + English + fields:
   │             numbers, surgeon, procedure, sponsored) + deterministic
   │             price-range check + regex 체험단|원고료|수수료|협찬 that
   │             overrides both LLMs                                  → out/validate.json
   ▼
   │ publish     out/published.json, out/quarantine.json (with reasons),
                 out/index.md (per-clinic table)                      → out/publish.json
```

Trust badges: `verified_surgeon` means the surgeon is on the clinic's registry roster.
`verified_procedure` means the clinic offers that procedure. `multi_source` means the review
was seen on 2 or more sources.

## Results of the committed run

- 8 posts in. 1 cross-post merged (p1 cafe.naver + p2 blog.naver, similarity 1.0 after normalisation), giving 7 reviews.
- Clinics: 7 resolved deterministically, including "서울미래성형외과", "미래성형", "Seoul Mirae PS" and "소린 성형외과".
  1 went to the LLM tie-break, which answered UNKNOWN ("그레이스뷰의원" is not in the registry).
- 5 published and 2 quarantined:
  - r3: a 체험단 / 원고료를 지원받아 sponsored contouring review.
  - r7: a "소정의 수수료" disclosure, and its clinic is not in the registry.
- See `out/index.md`.

## What broke and what I changed

On the first run, `claude -p` exited 1. The visible stderr was a settings-file permission warning,
but the real error was `Failed to authenticate: OAuth session expired and could not be refreshed`.
The pipeline now tries the CLI first, then falls back to the OpenClaw gateway, and records which
backend answered in each cache file. Details are in `NOTES.md`.
