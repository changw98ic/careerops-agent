# Recruitment crawl data processing runbook

## Scope

`scripts/process_recruitment_crawl_batches.py` is a local-only transformation
tool for existing recruitment crawl output. It does not invoke a crawler, make
network requests, schedule recurring work, or enable release actions.

It is offline preparation only. It does not satisfy the D0 gate, authorize M1
implementation, prove source lawfulness or retention compliance, enable
recurring crawling, Release Qualification, Gmail sends, Calendar writes, or
Auto-send.

## Inputs and outputs

The runner accepts either:

- `--crawl-output`: a directory containing
  `recruitment_pages_raw.jsonl` from `collect_recruitment_pages.py`; or
- `--sharded-batch`: a directory containing
  `sharded_recruitment_crawl_plan.json` from
  `run_sharded_recruitment_crawl.py`.

For sharded batches, the plan is treated as source-location metadata only. It
is not a completion signal. A shard without a raw JSONL file is reported as
`raw_output_not_created` and is picked up on a later run.

Use an explicit private or ignored output location. Derived records omit raw
`response_text`, but they still contain URLs, excerpts, provenance, and source
identifiers that require review before publication.

```bash
uv run python scripts/process_recruitment_crawl_batches.py \
  --crawl-output datasets/raw/recruitment_pages/2026-07-18/run14_sitemap_priority_v1 \
  --output-dir datasets/private/recruitment-processing/2026-07-19/run14
```

To inspect inputs without writing output:

```bash
uv run python scripts/process_recruitment_crawl_batches.py \
  --sharded-batch datasets/raw/recruitment_pages/2026-07-18/parallel_expansion_v10_endpoint_sharded_v1 \
  --output-dir datasets/private/recruitment-processing/2026-07-19/parallel-expansion-v10 \
  --dry-run
```

## Processing behavior

For every input source, the runner:

1. Captures the current raw-file byte size and reads only newline-terminated
   JSONL records through that snapshot.
2. Maintains an output-local byte and line watermark, so reruns consume only
   newly appended records.
3. Produces source-local recruitment signals and a source-local company rollup.
4. Merges all retained source signal files under the chosen output directory
   with deterministic URL/provenance deduplication.
5. Generates a combined company rollup.

One output directory has an advisory process lock. A concurrent invocation for
the same output directory fails rather than racing the source watermarks.
The runner never writes into a crawl output directory and never changes raw
captures or receipts.

The output layout is:

```text
<output-dir>/
  sources/<source-id>/           # per-source signal watermarks and outputs
  merged/                        # deduplicated recruitment_page_signals.jsonl
  company_rollup/                # combined recruitment_company_rollup.jsonl
  processing_manifest.json
  processing_summary.json
```

## Reruns and review

Rerun the same command and output directory as crawl data grows. Existing
source watermarks preserve incremental processing; the merged result includes
all retained source signal files in that output directory, not only the inputs
named in the latest invocation.

Review `processing_summary.json`, each source summary, and the final company
rollup before any promotion. Do not commit derived output until it has been
reviewed for private data, retention obligations, source terms, and
de-identification. Retain only the required evidence tuple when raw data is
purged under the applicable retention policy.

Stop and investigate if the runner reports an escaping sharded output path, a
source identity mismatch, a concurrent-processing lock, malformed middle JSONL
records, uncertain source legality, or uncertain handling of private data.
