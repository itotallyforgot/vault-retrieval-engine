---
title: Synth
last_updated: 2026-05-04
invoked_by: ["skills/vault/adapters/claude-code/vault.md"]
---

# Synth

**Purpose:** Surface candidate insights — themes, patterns, and
connections that have crossed multiple sources or topic pages and
deserve user attention. Outputs land as draft files under
`notes/` for the user to review, promote, or discard.

**When to run:** On demand by the user. Manual trigger only in MVP;
scheduled automation is deferred to a later phase.

**Inputs:** None required. Optional argument: `--since <ISO-date>` to
restrict the activity window (default: last 7 days). Optional:
`--max-clusters <N>` (default: 5) to cap output volume.

## Preconditions

- `notes/` directory exists (per `vault_map.md` and
  `notes/README.md`).
- `vault-engine` is installed and its MCP server is available — the
  procedure calls the `query_graph` tool. If the engine is missing or
  unreachable, surface the gap and stop.

## Procedure

### Phase 1 — Activity scan

1. Read `wiki/index.md` (catalog) and the last N entries of
   `wiki/log.md` (recent operations). N defaults to 50 lines or
   whatever covers the `--since` window.
2. Identify *active* topic pages — those touched (created, updated,
   or referenced from a new source) within the window.
3. Build a probe set: 5–15 query strings derived from the active
   topics' titles + aliases. These are the seeds for retrieval.

### Phase 2 — Cluster discovery

4. For each probe, call the engine MCP tool:
   ```
   query_graph(question=<probe>, top_k=20)
   ```
5. Aggregate the results. Group chunks by their topic / source page.
   A *cluster* is a candidate insight when **either**:
   - It spans ≥ 3 distinct sources within the activity window, OR
   - It spans ≥ 4 distinct topic pages with at least one new wikilink
     edge added in the window.
6. Rank clusters by (a) cross-page degree, (b) recency, (c) novelty
   (clusters whose topics had few prior wikilinks). Keep the top
   `--max-clusters` (default 5).

### Phase 3 — Per-cluster checkpoint loop

For each cluster, in rank order, run the following self-contained
sub-procedure. The work is **per-cluster idempotent**: if a later
cluster fails (rate limit, MCP timeout, partial write), the earlier
clusters' outputs are already on disk and committable independently.

**Per-cluster steps:**

7. **Pre-write conflict check.** Compute a candidate slug for this
   cluster (kebab-case from the dominant theme). Then:
   a. Glob `wiki/topics/*.md`. For each topic page, check whether the
      cluster's dominant theme matches the page title or any alias
      using the link-resolution normalization rule (case-insensitive,
      word-boundary, plural/morphology-tolerant — see `vault_map.md`).
   b. If a matching topic page exists → **DO NOT** write a new file
      under `notes/`. Instead, choose one:
      - **Append** a `## Candidate insight (YYYY-MM-DD)` section to
        the existing topic page summarizing the cluster's claim and
        citing sources, OR
      - **Flag** the conflict for user review with no append: print
        the topic path + the cluster summary and ask the user which
        action they prefer. Default to *flag* over *append* if the
        topic page already has a `## Synthesis` section maintained by
        lint (don't compete with lint).
   c. If no matching topic exists → proceed to step 8.
8. **Write the draft.** Create `notes/YYYY-MM-DD-<slug>.md`
   with the frontmatter contract documented in
   `notes/README.md`:
   ```yaml
   ---
   title: "..."
   tags: [insight, draft]
   sources: [<links to source pages, topic pages, or raw paths>]
   status: draft
   generated_at: <ISO 8601 timestamp with timezone>
   generated_by: "synth-claude-code"
   last_updated: YYYY-MM-DD
   ---
   ```
   Body: a concise summary of the cluster's claim, supported by
   citations to vault material. No new claims un-anchored to existing
   sources. Length target: 150–400 words.
9. **Append to log.** Add one line to `wiki/log.md`:
   ```
   ## [YYYY-MM-DD HH:MM] synth | <slug> | <action: drafted|appended|flagged>
   ```
10. **Commit checkpoint (optional but encouraged).** If running in a
    git-aware context, commit each cluster's outputs immediately so a
    later failure leaves a clean trail.

### Phase 4 — Final summary

11. Append a closing line to `wiki/log.md`:
    ```
    ## [YYYY-MM-DD HH:MM] synth | run summary | drafted=A appended=B flagged=C skipped=D
    ```
12. Print to the user: per-cluster outcomes (slug, action,
    `notes/...` path or topic path), and a one-line next-step
    suggestion: *"Review drafts under notes/. Promote with
    /vault ingest semantics or discard."*

## Out of scope

- Modifying existing topic page content beyond the optional
  `## Candidate insight` append section. Wiki synthesis at the topic
  level is owned by `/vault lint` (Synthesis sections on hub pages).
- Writing to `raw/`. Synth never produces raw sources.
- Promotion logic. Drafts are never auto-promoted; the user chooses.
- Deleting drafts. Discarded drafts get `status: discarded`; deletion
  is a manual action or future Phase 1.5 helper.
- Rate-limit retry. If a Max-plan rate limit hits mid-loop, the
  per-cluster checkpoint guarantees recoverability — the user
  re-invokes `/vault synth` later and the loop skips already-drafted
  themes (see step 7's conflict check).

## Validation

After the run, all of the following must hold:

- Every new file under `notes/` has valid frontmatter
  matching the contract in `notes/README.md`.
- No file under `notes/` duplicates an existing topic
  (cross-check by computing the same conflict-detection rule from
  step 7 for every newly-written draft).
- `wiki/log.md` contains per-cluster entries plus a final summary
  line for this run.
- No file under `wiki/topics/` had its body rewritten — only the
  optional `## Candidate insight (YYYY-MM-DD)` section was appended
  where applicable.
- No file under `raw/` was touched.

## Failure modes

| Failure | Recovery |
|---|---|
| Engine MCP unreachable (Phase 2 step 4 returns error) | Stop with a clear error. No partial wiki writes (Phase 3 hasn't started). |
| Mid-loop rate limit | Stop after the current cluster's checkpoint commits. Re-invoke later; the conflict check in step 7 prevents re-drafting the same theme. |
| Conflict-check ambiguous | Default to *flag*, not *append* or *write*. Surface to user. |
| Probe returns empty top-K | Skip that probe. Note in log summary. |
| Draft body is empty / un-citable | Discard the draft for that cluster (don't write the file). Note in log. |

## Reference

- Spec and plan: tracked in the vault owner's internal planning notes
  (not included in this repo).
- Output convention: `notes/README.md`
- Vault structure: `vault_map.md`
