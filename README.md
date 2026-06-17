# arXiv Metadata Service

Local-only arXiv metadata query service over the Kaggle/Cornell arXiv OAI
snapshot. The primary workflow builds a SQLite database from a local JSONL
snapshot and serves a localhost HTTP API.

This service does not require MCP. Automatic download is disabled in the
default workflow.

## Paths

Default local snapshot:

```text
/Volumes/My Book/ARXIV/arxiv-metadata-oai-snapshot.json
```

Default SQLite database:

```text
/Volumes/My Book/ARXIV/arxiv_oai_title_fts.sqlite
```

## Required Preflight

Before running `arxiv-meta`, check the copied service code and the DB path that
the command will use. Do not assume the Desktop copy or optional Desktop DB is
present.

For commands run from the Paper_DB Desktop copy against the canonical external
DB:

```bash
test -f "/Users/xjp/Desktop/Paper_DB/ARXIV/arxiv-metadata-service/pyproject.toml"
test -f "/Volumes/My Book/ARXIV/arxiv_oai_title_fts.sqlite"
```

For rebuilds, also check the source snapshot:

```bash
test -f "/Volumes/My Book/ARXIV/arxiv-metadata-oai-snapshot.json"
```

For commands that use the optional Desktop DB copy, first check:

```bash
test -f "/Users/xjp/Desktop/Paper_DB/ARXIV/db/arxiv_oai_title_fts.sqlite"
```

## Setup

```bash
uv sync
```

The project is pinned to Python 3.12 through `.python-version` and
`requires-python`.

On ExFAT workspaces, `uv` may print:

```text
Failed to acquire environment lock: Operation not supported
```

This is an environment-lock warning from the filesystem, not an application
failure. Tests can still pass. To avoid the warning, keep the project
environment on a local APFS path, for example by creating the virtual
environment on internal storage and pointing tooling at it, or by using a
project-env/cache workaround outside the ExFAT volume.

## Configuration

Implemented read-mode options live under `db.read` in `config.yaml`:

```yaml
db:
  read:
    immutable: true
    query_only: true
    mmap_size: 268435456
    cache_size: -80000
```

- `immutable`: adds `immutable=1` to the SQLite read URI. Use it only when no
  writer is modifying the DB file.
- `query_only`: applies `PRAGMA query_only=ON` to normal search/API read
  connections.
- `mmap_size`: applies `PRAGMA mmap_size`; the value is bytes.
- `cache_size`: applies `PRAGMA cache_size`; negative values are KiB.

Environment overrides are available:

```text
ARXIV_META_DB_IMMUTABLE
ARXIV_META_DB_QUERY_ONLY
ARXIV_META_DB_MMAP_SIZE
ARXIV_META_DB_CACHE_SIZE
```

## Build

```bash
uv run arxiv-meta build \
  --jsonl "/Volumes/My Book/ARXIV/arxiv-metadata-oai-snapshot.json" \
  --db "/Volumes/My Book/ARXIV/arxiv_oai_title_fts.sqlite"
```

The importer streams the JSONL input one line at a time. It creates:

- `papers`
- `paper_categories`
- `title_fts`
- `paper_fts`
- `import_runs`

`title_fts` indexes titles only. Abstract text is stored for responses but is
not indexed for plain title keyword search. `paper_fts` indexes `title`,
`abstract`, and `authors` for the normalized Boolean backend.

For external ExFAT targets, build on an internal staging directory and copy the
completed SQLite file into place:

```bash
uv run arxiv-meta build \
  --jsonl "/Volumes/My Book/ARXIV/arxiv-metadata-oai-snapshot.json" \
  --db "/Volumes/My Book/ARXIV/arxiv_oai_title_fts.sqlite" \
  --staging-dir /tmp
```

## Serve

```bash
uv run arxiv-meta serve \
  --host 127.0.0.1 \
  --port 8110 \
  --db "/Volumes/My Book/ARXIV/arxiv_oai_title_fts.sqlite"
```

The default host is `127.0.0.1`. Binding to `0.0.0.0` only happens if the user
passes that host explicitly or changes configuration.

## Smoke

```bash
uv run arxiv-meta smoke \
  --db "/Volumes/My Book/ARXIV/arxiv_oai_title_fts.sqlite"
```

## API

### `GET /health`

Returns service health and paper count.

### `GET /stats`

Returns total papers, DOI coverage, journal reference coverage, and database
size.

### `GET /arxiv/{arxiv_id}`

Looks up one paper by exact arXiv ID. Missing IDs return 404.

### `GET /search`

Searches title text.

Parameters:

- `q`: required title keywords, max 256 characters.
- `limit`: default 50, max 500.
- `cat`: optional comma-separated exact category tokens, for example `cs.CL`.
- `update_date_from`: optional inclusive `YYYY-MM-DD`.
- `update_date_to`: optional inclusive `YYYY-MM-DD`.
- `sort`: `relevance` or `date`.

Example:

```bash
curl "http://127.0.0.1:8110/search?q=taxonomy%20generation&cat=cs.CL&update_date_from=2024-01-01"
```

Pasted full titles are treated as plain title text. Common punctuation such as
colons, dashes, parentheses, and quotes is sanitized into safe token
boundaries. Raw advanced FTS syntax is not exposed.

### `POST /boolean-search`

Searches with an already normalized Boolean query object. This endpoint does
not parse raw query strings; parser and source-neutral field mapping live
outside this service. The endpoint is separate from `/search`, so existing
plain title search behavior is unchanged.

Supported Boolean operators:

- `AND`
- `OR`
- `ANDNOT`

Supported text fields:

- `title`
- `abstract`
- `authors`
- `all`

Supported metadata/range fields:

- `category`
- `primary_category`
- `arxiv_id`
- `update_date`
- `first_version_date`

Example:

```bash
curl -X POST "http://127.0.0.1:8110/boolean-search" \
  -H "Content-Type: application/json" \
  -d '{
    "query_object": {
      "op": "AND",
      "children": [
        {"field": "abstract", "match": {"type": "term", "value": "taxonomy"}},
        {"field": "category", "match": {"type": "term", "value": "cs.CL"}}
      ]
    },
    "limit": 5
  }'
```

Response includes the original `query_object`, `mode: boolean`, a compact
`compiled` summary of fields/operators, `total`, and `results`.

The equivalent CLI command accepts a normalized JSON file:

```bash
uv run arxiv-meta boolean-search query.json \
  --db "/Volumes/My Book/ARXIV/arxiv_oai_title_fts.sqlite" \
  --limit 5
```

For existing databases created before `paper_fts`, create or refresh the
multi-field FTS index with:

```bash
uv run arxiv-meta rebuild-paper-fts \
  --db "/Volumes/My Book/ARXIV/arxiv_oai_title_fts.sqlite"
```

### `GET /candidate-search`

Returns review candidates for unmatched titles. This mode uses SQLite FTS5
phrase anchors and bounded token fallback for candidate retrieval, then returns
token-overlap evidence. It does not auto-accept results and does not change
`/search` semantics.

Parameters:

- `q`: required unmatched title text, max 256 characters.
- `limit`: default 20, max 500.
- `cat`: optional comma-separated exact category tokens.
- `update_date_from`: optional inclusive `YYYY-MM-DD`.
- `update_date_to`: optional inclusive `YYYY-MM-DD`.
- `include_details`: optional, default false. When true, the service first
  retrieves lightweight candidates and then fetches full paper details by
  `arxiv_id`.

Response items include:

- `source_id`
- `title`
- `categories`
- `update_date`
- `score`
- `shared_tokens`
- `evidence`
- `review_candidate: true`
- `auto_accept: false`

Example:

```bash
curl "http://127.0.0.1:8110/candidate-search?q=taxonomy%20metadata%20unmatched&limit=5"
```

The equivalent CLI command is:

```bash
uv run arxiv-meta candidates \
  "taxonomy metadata unmatched" \
  --db "/Volumes/My Book/ARXIV/arxiv_oai_title_fts.sqlite" \
  --limit 5
```

### `POST /candidates-batch`

Runs multiple review-candidate queries with one SQLite connection.

Request:

```json
{
  "default_limit": 20,
  "include_details": false,
  "items": [
    {
      "request_id": "row-1",
      "query": "Many-shot Jailbreaking",
      "limit": 20,
      "categories": ["cs.LG"],
      "update_date_from": null,
      "update_date_to": null,
      "include_details": false
    }
  ]
}
```

Response:

```json
{
  "mode": "candidate_batch",
  "auto_accept": false,
  "total": 1,
  "results": [
    {
      "request_id": "row-1",
      "query": "Many-shot Jailbreaking",
      "limit": 20,
      "total": 1,
      "auto_accept": false,
      "results": [
        {
          "source_id": "2504.09604",
          "title": "Mitigating Many-Shot Jailbreaking",
          "categories": ["cs.LG", "cs.AI", "cs.CR"],
          "update_date": "2026-03-26",
          "score": -17.78,
          "shared_tokens": ["many", "shot", "jailbreaking"],
          "review_candidate": true,
          "auto_accept": false
        }
      ]
    }
  ]
}
```

The equivalent CLI command reads JSONL or JSON:

```bash
uv run arxiv-meta candidates-batch \
  "/Users/xjp/Desktop/TaxoBench-CS/analysis_artifacts/taxobench_paper_audit_2026-06-01/missing_ref_metadata/metadata_downloads/snapshot_arxiv_oai_fuzzy_trial50_normalized_title/review_candidates.jsonl" \
  --db "/Volumes/My Book/ARXIV/arxiv_oai_title_fts.sqlite" \
  --limit 20
```

### `POST /batch-doi`

Looks up DOI values.

Limits:

- Maximum 500 DOI strings per request.
- Maximum DOI length is 256 characters.

## Error Model

Malformed title queries return HTTP 400:

```json
{
  "error": "invalid_fts_query",
  "message": "The title query could not be parsed."
}
```

Invalid date filters return HTTP 400:

```json
{
  "error": "invalid_date",
  "message": "Expected YYYY-MM-DD."
}
```

Oversized query requests return HTTP 400:

```json
{
  "error": "request_too_large",
  "message": "Maximum DOI batch size is 500."
}
```

## Benchmark

Implemented benchmark script:

```bash
uv run python scripts/benchmark_candidates.py \
  --input "/Users/xjp/Desktop/TaxoBench-CS/analysis_artifacts/taxobench_paper_audit_2026-06-01/missing_ref_metadata/metadata_downloads/snapshot_arxiv_oai_fuzzy_trial50_normalized_title/review_candidates.jsonl" \
  --db "/Volumes/My Book/ARXIV/arxiv_oai_title_fts.sqlite" \
  --limit 20 \
  --max-rows 20 \
  --category-mode primary \
  --summary-only
```

Use `--output PATH` to write the full candidate results and benchmark summary
as JSON.

## FTS Optimize

Implemented maintenance command:

```bash
uv run arxiv-meta optimize-fts \
  --db "/Volumes/My Book/ARXIV/arxiv_oai_title_fts.sqlite"
```

This runs FTS5 `optimize` and records DB size before/after plus elapsed time.
It does not run `VACUUM`.

## Implemented In Performance Phase A

- Optimized read-only SQLite connections with `mode=ro`, optional
  `immutable=1`, `query_only`, `mmap_size`, and `cache_size`.
- `GET /candidate-search` uses lightweight first-stage candidate SELECTs.
- Optional candidate detail fetch by `arxiv_id` through `include_details`.
- `POST /candidates-batch` and `arxiv-meta candidates-batch`.
- `scripts/benchmark_candidates.py` for the TaxoBench 20-row workload.
- `arxiv-meta optimize-fts` for FTS5 optimize without VACUUM.

## Planned Or Deferred

Full fuzzy title matching and acceptance decisions are deferred. The implemented
candidate search is a review-candidate retrieval workflow only and must not
auto-accept matches.

MCP integration is deferred and disabled by default. Reintroducing MCP would
require an explicit opt-in dependency and no runtime package installation.

Additional deferred work:

- Frequency-aware candidate gating.
- Broader benchmark suites beyond the 20-row TaxoBench smoke workload.
- VACUUM proposal with separate space/time risk assessment.

## Non-Goals

- No frequency gating in Performance Phase A.
- No stopword removal.
- No change to `/search` semantics.
- No conversion of candidate retrieval into formal OR/fuzzy search.
- No candidate auto-accept.
- No MCP enablement.
- No VACUUM.
