# Build log: real failures only

## 1. Headless `claude -p` could not authenticate (first run)

- **Symptom:** first `python3 pipeline.py run` died in the extract step:
  `RuntimeError: claude exited 1: Permission allow rule (../../../.claude/settings.json): Write(//User…bit/**) is not matched by file permission checks — only Edit(path) rules are. ...`
- **Misleading part:** that stderr line is only a settings *warning*. Running the CLI by hand from `/tmp`
  showed the actual error on the next line: `Failed to authenticate: OAuth session expired and could not be refreshed`.
  `claude auth status` reported `"loggedIn": false`.
- **Things that did not fix it:** `--setting-sources project` (removes the warning, same auth error);
  unsetting the nested-session env vars (`CLAUDECODE`, `CLAUDE_CODE_*`, `CLAUDE_PID`): same auth error.
  Logging in again needs a browser, which a headless run can't do.
- **Fix:** `llm_json()` still tries `claude -p --model sonnet --output-format text` first (prompt on stdin, 180 s timeout).
  If that exits non-zero, it logs the CLI's last stderr line and sends the same prompt through the local
  OpenClaw gateway: `openclaw agent --session-id gbg-<sha>-<ts> --message-file <prompt> --json`.
  The gateway holds its own Anthropic credentials, and the reply text comes from the `terminalReply.text` field.
  Each cache file records which backend answered (`"backend": "openclaw-gateway"` in this run).
  I also changed the error message so it shows the last stderr line, not the first, because the first line hid the real cause.

## 2. Things I checked that did NOT break (recorded so nobody assumes they did)

- The LLM returned bare JSON for all 3 calls. `parse_json_loose` (strip fences, decode from the first `[`/`{`)
  is still in place, but it wasn't needed this run.
- 만원 conversion: all 7 extracted prices were correct, including p5 ("회당 8만원, 총 80만원" → 800000, the total).
- Dedupe on the original Korean: the raw 3-gram Jaccard of the p1/p2 cross-post is 0.833. After stripping
  emoji and whitespace it is 1.0, so it merges easily above the 0.8 bar.
- The sponsored regex and both LLMs agreed on every review (`regex_overrides` is empty everywhere).
