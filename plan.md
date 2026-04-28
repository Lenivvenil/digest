# Plan: Issue #64 — Domain-reviewer pass on irritator-bc.md

## 1. Problem restatement

`docs/domain/irritator-bc.md` was authored by the domain-researcher agent as part of the issue #53 fix (commit `8d36567`), with `[UPDATED — issue #53]` and `[REMOVED — issue #53]` markers applied inline to schema and config facts. However, the "Where the BC breaks" section (lines 96–114) lists 8 pipeline failure modes as if all are current — it received no markers. In reality, 4 of the 8 were addressed directly in the same commit (adversarial query phrasing, ranker AND-logic → single criterion with calibration anchors, score threshold 7→5, dev.to text-search fix). A reader today cannot tell which failures are historical and which remain open, making the section misleading for anyone debugging or extending the irritator.

## 2. Affected bounded contexts and files

- **Irritator BC** — `docs/domain/irritator-bc.md` (the only file being changed)
- No code files are touched; no BC boundary or inter-context contract changes.

## 3. Considered approaches

### A. In-place fix-status markers
Add `[FIXED — issue #53]` labels to the four resolved failure points, leave the remaining four untouched.

**Pro:** Minimal diff, consistent with the `[UPDATED]`/`[REMOVED]` pattern already in the file. Preserves full diagnostic history in place.  
**Con:** Each failure point is a multi-paragraph structural analysis, not a one-line schema fact. A `[FIXED]` tag on a bold heading does not tell the reader whether the point is a completed historical lesson or a live open issue — they still must read the entire paragraph and infer. Option A works for one-line annotations; it does not carry the framing well for paragraph-length diagnostics.

### B. Split into two subsections (chosen)
Restructure "Where the BC breaks" into two explicit subsections: **"Fixed by issue #53 (historical record)"** and **"Still open."** Fixed items stay verbatim under the historical header; open items stay under the live header. Add a one-sentence framing note at the top.

**Pro:** Structure makes current vs. historical immediately visible without reading each item. A domain-reviewer, implementer, or future issue author scanning for live failures lands directly on the "Still open" subsection.  
**Con:** Slightly larger diff; moves content rather than annotating it.

### C. Delete fixed items entirely
Remove points 1, 3, 4, 5 entirely.

**Con:** Loses the causal reasoning behind the fixes (adversarial phrasing rationale, calibration anchor motivation, threshold logic). Ruled out — diagnostic value is worth keeping.

**Chosen:** B. Option A is locally consistent but poorly suited to paragraph-length content; Option C destroys historical context. B is the only option that makes the section accurate and scannable.

## 4. Chosen approach and why

Verified against git diff of commit `8d36567` (the actual #53 fix):

**Fixed by issue #53 — 4 points:**
- Point 1: Query generator adversarial phrasing (`query_generator.py` rewritten with contradiction patterns)
- Point 3: Ranker AND-logic (`ranker.py` — four-criteria conjunction replaced with single "CONTRADICTS or COMPLICATES" criterion + 9-10/7-8/5-6/1-4 calibration anchors)
- Point 4: `min_signal_score=7` threshold (`config.py` — default 7→5, consistent with prod override)
- Point 5: dev.to adapter (`devto.py` — `?tag=` replaced with `?q=` full-text search)

**Still open — 4 points:**
- Point 2: Narratives extracted from a curated pro-tech feed (no feed or prompt change)
- Point 6: Source-narrative fit unmodelled (fan-out added, but source profiling not implemented)
- Point 7: All-pairs ranking blurs per-narrative results (provenance still dropped at `search_all_sources()`)
- Point 8: No feedback loop (unchanged)

This maps to the issue's option **(a): "Keep as historical record (label it explicitly)"** — fixed points are preserved verbatim under a historical subheading, not deleted.

Also add a cross-reference from the "Still open" items to §3 "Missing concepts" where these gaps are already documented as design targets, to prevent readers treating them as undiscovered bugs.

No ADR triggered: doc-only change, no BC boundary or inter-context contract change, no new dependency. Per `docs/principles.md` § "Что значит «архитектурно-значимо»": this is a story, not a decision.

## 5. Test strategy

Documentation-only change — no runtime code is touched.

**Acceptance criterion (issue #64):** `domain-reviewer` agent returns APPROVE verdict on the updated file.

**Manual verification during implement:**
- Points 1, 3, 4, 5 classified as fixed — confirmed against `git show 8d36567` diff of `query_generator.py`, `ranker.py`, `config.py`, `devto.py`.
- Points 2, 6, 7, 8 still open — no corresponding code changes in any commit on `main`.

No unit/integration tests: CI does not cover `.md` files.

## 6. Risks and unknowns

- **"Still open" list risks raising expectations.** Points 2, 6, 7, 8 are architectural gaps already documented in §3 "Missing concepts" as future-work design targets. Labelling them "still open" without context could make them look like tracked near-term bugs. Mitigation: cross-reference to §3 explicitly.
- **domain-reviewer may flag vocabulary drift or concept collisions** beyond the scope of this fix (the file has known issues documented in §1 "Vocabulary drift"). Those findings should open a new issue, not block this PR. The APPROVE criterion here is specifically for the accuracy of the failure-mode classification.
