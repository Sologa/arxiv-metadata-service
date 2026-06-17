# Boolean Backend Tasks

Status: initial backend implementation complete

## Guardrails

- Work only inside `/Volumes/My Book/ARXIV/arxiv-metadata-service`.
- Do not edit `/Users/xjp/Desktop/Paper_DB/CLI_MANUAL.md`.
- Do not implement the raw query string parser in this repo.
- Do not implement source-neutral S2 or ACL field mapping in this repo.
- Preserve existing `/search`, `candidates`, and `candidates-batch` semantics.
- Add advanced Boolean backend behavior through a separate method, command, or
  endpoint so existing callers are not surprised.

## Architecture Split To Preserve

- [x] Layer 1: parser layer lives outside this repo for now.
- [x] Layer 2: source-neutral field mapping / capability layer lives outside
      this repo for now.
- [x] Layer 3: arXiv SQLite backend compiler lives in this repo.

## Phase 0: Contract

- [x] Define the first normalized query object shape.
- [x] Add typed helpers or dataclasses for query nodes if useful.
- [x] Document which fields are accepted by the arXiv backend.
- [x] Reject unsupported fields with controlled errors.
- [x] Reject unsupported match types with controlled errors.

## Phase 1: Multi-Field FTS

- [x] Add a new FTS5 table for searchable paper text, for example
      `paper_fts(title, abstract, authors)`.
- [x] Keep `title_fts` compatibility unless the migration plan explicitly
      proves it is safe to replace.
- [x] Update database build code to populate or rebuild the new FTS table.
- [x] Add tests proving abstract-only terms are searchable through the new
      backend, while existing title-only `/search` behavior is unchanged.
- [x] Add a maintenance path for the new FTS table if needed.

## Phase 2: Backend Compiler

- [x] Implement text node lowering to FTS5.
- [x] Implement category node lowering to `paper_categories`.
- [x] Implement date range lowering to `papers.update_date` and
      `papers.first_version_date`.
- [x] Implement `AND` over child result sets.
- [x] Implement `OR` over child result sets.
- [x] Decide whether `NOT` or `ANDNOT` is supported in version 1.
- [x] Add a conservative query limit and result limit for the advanced backend.
- [x] Return structured provenance for how each normalized query was compiled.

## Phase 3: Interface

- [x] Add an internal `ArxivSearch` method for normalized Boolean queries.
- [x] Add a maintenance CLI for rebuilding `paper_fts` on existing databases.
- [x] Add a Boolean query CLI for normalized JSON query files.
- [x] Add an HTTP endpoint only if it can be kept separate from existing
      `/search` semantics.
- [x] Include `query_object`, `mode`, `total`, and `results` in the response.

## Phase 4: Tests

- [x] Unit test title phrase search.
- [x] Unit test abstract term search.
- [x] Unit test author term search.
- [x] Unit test category exact-token search.
- [x] Unit test date range filters.
- [x] Unit test `(title OR abstract) AND category`.
- [x] Unit test `(category A OR category B) AND date range`.
- [x] Unit test unsupported field errors.
- [x] Unit test unsupported match type errors.
- [x] Regression test existing invalid raw FTS syntax remains rejected by
      existing `/search`.

## Phase 5: Real DB Smoke

- [x] Run `uv run pytest`.
- [x] Run a small real DB smoke query against:
      `/Volumes/My Book/ARXIV/arxiv_oai_title_fts.sqlite`.
- [x] Compare one local query shape against a generated local DB smoke query
      only as a sanity check, not as an exact equivalence test.
- [x] Record known differences from live arXiv API behavior.
