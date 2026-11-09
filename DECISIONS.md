# Decisions

Selim's notes. The facts (what was decided, what was rejected, where it is in the code) were
drafted while building; each **In my own words** line is mine to write.

Commit links point to `github.com/sxlimsxleh/procure-agent-lite/commit/<sha>`.

## Decisions

### D1. The LLM extracts; code decides; a person approves money
- **Decision:** one Claude call per document with a single tool, `record_quote`, that only
  records data. Exclusions, scoring and the recommendation are code (`decide.py`). Nothing is
  written to SAP without an approval.
- **Rejected:** LLM chooses the supplier; an agent framework with a tool loop.
- **Evidence:** `tests/test_decide.py::test_injection_doc_never_wins_even_when_cheapest`,
  `test_rationale_never_changes_the_recommendation`.
- **Commits:** [9fe3b41](https://github.com/sxlimsxleh/procure-agent-lite/commit/9fe3b41), [059c033](https://github.com/sxlimsxleh/procure-agent-lite/commit/059c033)
- **In my own words:** …

### D2. Grounded or flagged, by checking possible readings
- **Decision:** a value is accepted only if its source text is in the document and one possible
  reading of a number/date in that text equals the value. The code never guesses the locale.
- **Rejected:** trusting structured output; per-supplier regexes; an LLM-as-judge check.
- **Evidence:** `tests/test_ground.py`; the fax document, where the model "repaired" two values
  in both live runs and grounding rejected them.
- **Commit:** [9fe3b41](https://github.com/sxlimsxleh/procure-agent-lite/commit/9fe3b41)
- **In my own words:** …

### D3. Idempotency key from business facts; approval queue derived from an append-only log
- **Decision:** `sha256("pr_id|supplier_id|total_eur")`; the run status is replayed from audit
  events, and the database blocks UPDATE/DELETE on them.
- **Rejected:** random key per request; check-then-create; a mutable status column.
- **Evidence:** `tests/test_erp.py::test_flaky_erp_with_forced_retries_creates_exactly_one_po`
  and the control test `test_without_stable_key_retries_create_duplicates`;
  `tests/test_audit.py::test_log_is_append_only`.
- **Commits:** [6ea4a77](https://github.com/sxlimsxleh/procure-agent-lite/commit/6ea4a77), [32e2789](https://github.com/sxlimsxleh/procure-agent-lite/commit/32e2789)
- **In my own words:** …

### D4. Recorded model responses, with a prompt fingerprint, so CI needs no key
- **Decision:** live runs save each raw model answer; CI replays them through the same code. A
  fingerprint of prompt + schema makes outdated recordings fail instead of reporting old numbers.
- **Rejected:** calling the API in CI (cost, flakiness, secrets in every PR); mocking the model
  with hand-written answers only.
- **Commit:** [91452c6](https://github.com/sxlimsxleh/procure-agent-lite/commit/91452c6)
- **In my own words:** …

### D5. The eval gate is "no wrong value accepted", not an accuracy threshold
- **Decision:** correctly flagging a missing value counts as correct; CI fails on any wrong value
  marked as verified. Wrongly flagged values are reported but do not fail the build.
- **Rejected:** a single accuracy percentage as the gate (a high number can hide one costly error).
- **Commit:** [91452c6](https://github.com/sxlimsxleh/procure-agent-lite/commit/91452c6)
- **In my own words:** …

### D6. Unreadable OCR values are flagged, even when the total would "confirm" them
- **Decision:** `4,4б` stays flagged, although 4.40 × 500 matches the stated total. Inferring it
  would be a calculation, and extraction must never calculate.
- **Rejected:** accept a value when it is consistent with the total.
- **Commit:** [7b285bf](https://github.com/sxlimsxleh/procure-agent-lite/commit/7b285bf)
- **In my own words:** …

## Where the AI got it wrong

### W1. The original spec assumed API features that current models reject
- **What happened:** the project brief required `temperature 0` and a forced `tool_choice`.
  Current Claude models reject both.
- **How it was caught:** checking current API documentation before writing the extraction code.
- **What changed:** `tool_choice: auto` + `strict: true` + an error result when no tool call comes
  back; repeatability from recordings instead of temperature (CLAUDE.md rules 3 and 7).
- **Commit:** [9fe3b41](https://github.com/sxlimsxleh/procure-agent-lite/commit/9fe3b41)
- **In my own words:** …

### W2. The dataset was not reproducible on another machine
- **What happened:** the generator used the operating system's font; GitHub's runner has a
  different version, so the regenerated PDFs differed and CI failed.
- **How it was caught:** the test that regenerates the dataset and compares bytes, on the first
  CI run.
- **What changed:** the font is vendored in the repo.
- **Commit:** [bb04c3c](https://github.com/sxlimsxleh/procure-agent-lite/commit/bb04c3c)
- **In my own words:** …

### W3. A wrong claim about which supplier would win
- **What happened:** the plan for milestone 4 claimed that reading Müller's page-1 list price
  instead of the page-2 discount would make Schmidt win.
- **How it was caught:** the test written for that claim failed: Müller still wins on delivery
  date. The real cost of the error is a PO €195 too high.
- **What changed:** the test now asserts the €195 difference.
- **Commit:** [059c033](https://github.com/sxlimsxleh/procure-agent-lite/commit/059c033)
- **In my own words:** …

### W4. A retry test that assumed retries always succeed
- **What happened:** the first version of the "exactly one PO" test failed: with its random seed
  the mock SAP failed 10 times in a row, and the client gave up, although one of those failures
  had already created the PO.
- **How it was caught:** the test failed.
- **What changed:** the seed was kept and the test now requires exactly that case (client gives
  up, buyer clicks again, still one PO); checked separately across 300 seeds.
- **Commit:** [6ea4a77](https://github.com/sxlimsxleh/procure-agent-lite/commit/6ea4a77)
- **In my own words:** …

### W5. UI bugs that no test caught
- **What happened:** after a PO was created, Approve and Reject were still shown, and the audit
  table was full of "None".
- **How it was caught:** reviewing screenshots of the running UI, not by tests.
- **What changed:** both fixed; a UI test now checks that closed runs show no actions.
- **Commit:** [32e2789](https://github.com/sxlimsxleh/procure-agent-lite/commit/32e2789)
- **In my own words:** …

### W6. The number reader accepted part of an OCR-garbled number
- **What happened:** `4,4б` was read as 4.4, so a guessed 4.40 would have been accepted as
  verified.
- **How it was caught:** probing the grounding check while building the OCR test document,
  before any model run hit it.
- **What changed:** numbers touching a letter or digit are not read at all.
- **Commits:** [7b285bf](https://github.com/sxlimsxleh/procure-agent-lite/commit/7b285bf), [ce58f06](https://github.com/sxlimsxleh/procure-agent-lite/commit/ce58f06)
- **In my own words:** …

### W7. A schema change that only the real API could reject
- **What happened:** adding two nullable fields made 18; strict schemas allow 16. All local tests
  passed; the first live request returned 400.
- **How it was caught:** the live eval run (it failed on the first request, so nothing was spent).
- **What changed:** three fields got plain defaults; a test now counts nullable fields against the
  limit.
- **Commit:** [6d04fcb](https://github.com/sxlimsxleh/procure-agent-lite/commit/6d04fcb)
- **In my own words:** …
