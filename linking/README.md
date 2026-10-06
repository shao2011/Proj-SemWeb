# Automatic Wikidata-only Books linking (v2)

This additive stage reads `pipeline/output/books_data.ttl`, preserves it and
`ontology.ttl`, and writes only accepted Wikidata QID identities as
`owl:sameAs`. Its only semantic decisions are `ACCEPTED` and `NO_MATCH`.
Operational failures stay in `errors.jsonl`, are retried, and block final RDF
materialization. There is no manual review or labeling workflow.

The seven targets are `BookEdition`, `Book`, `Person`, `BookSeries`, `Publisher`,
`Language`, and `Place`. An asserted `AudiobookEdition` is handled through its
`BookEdition` type. Open Library is absent from the production execution path:
there are no Open Library API calls, dumps, indexes, identity URIs, or P648
bridges. The independent ISBN coverage experiment remains isolated under
`linking/experiment_wikidata_isbn.py` and `linking/output/wikidata_isbn_experiment/`.

## Set up and test

Run from the repository root. This environment is separate from `pipeline/.venv`.

```bash
uv venv linking/.venv
uv pip install --python linking/.venv/bin/python -r linking/requirements.txt
linking/.venv/bin/python -m pytest -q linking/test_linking.py
pipeline/.venv/bin/python -m pytest -q pipeline/test_pipeline.py
```

## Matching paths

| Local target | Wikidata candidate generation | Required verification |
| --- | --- | --- |
| BookEdition | Batched exact ISBN-13 `P212`; valid original ISBN-10 `P957` | Edition/publication type, title sanity, explicit metadata conflicts; no title-search fallback |
| Book | Accepted Edition QID → `P629` and inverse `P747` union | One convergent Work QID, Work-level type, title and available `P50` author context |
| Author Person | Accepted Work QID → `P50` | Human type, name/aliases, compatible occupation when stated |
| BookSeries | Accepted member Work QID → `P179` | Series type, label/aliases, ambiguity margin |
| Publisher | Accepted Edition QID → `P123` | Publisher type, label/aliases, parent/imprint conflicts |
| Language | `wbsearchentities` | Language type, very high label similarity and margin |
| Place | `wbsearchentities` | Place type, very high label similarity, uniqueness and margin |

The ISBN validator checks ISBN-10 and ISBN-13 checksums and the 978/979 ISBN-13
prefix. An invalid ISBN never becomes evidence. Indexed exact-value batches
probe compact and conventional hyphen/space groupings; every returned ISBN is
normalized and validated again. Unusual mixed separator layouts may be missed.
An Edition may be accepted without a Work relation. A Book never uses title
search to fill a missing Work path and never links to an Edition item.

A Person without an Author role or accepted Work anchor, a Series without an
accepted member Work, and a Publisher without an accepted Edition cannot pass.
If the needed `P50`, `P179`, or `P123` relation is absent, they return
`NO_MATCH`/`INSUFFICIENT_CONTEXT` without text search. Dual-role Authors may
pass through their Author context. The Person linker is strongest for Authors.
Non-author contributor roles are only accepted when strong explicit contextual
evidence is available; otherwise they remain NO_MATCH. Version 2 has no such
bridge for pure Translators, Illustrators, Editors, or Narrators.

Scores use fixed design weights, target thresholds, hard conflicts, and a
best-versus-runner-up margin. Several plausible ISBN candidates or conflicting
Edition → Work QIDs return `NO_MATCH`. Name similarity cannot replace a
mandatory structured relation. Language and Place keep conservative direct
label/type matching. If another accepted Work or Edition explicitly points to
a different `P50`, `P179`, or `P123` identity for the same local entity, that
contradictory anchor vetoes acceptance.

## Bounded live sample, smoke, and freeze

Set `LINKING_CONTACT` in the environment to a real contact address. It is not
stored in code or config. The client makes sequential Wikidata API/SPARQL
requests, honors `429 Retry-After`, retries transient errors, and caches
successful responses in `linking/output/wikidata_state.sqlite3`. The sample is
bounded and does not write production RDF links.

```bash
export LINKING_CONTACT='your-contact@example.org'
linking/.venv/bin/python linking/link_books.py sample
linking/.venv/bin/python linking/link_books.py smoke
linking/.venv/bin/python linking/link_books.py freeze
```

The fixed seed is `20261004`. The sample includes a small random slice plus
eight known ISBN regression anchors from the independent experiment, their
parent Books, and authors. `smoke` compares the five known positive
Edition → Work paths and three negative/ambiguous cases against the just-run
sample; it records observations, never production links.

Inspect:

- `linking/output/sample/diagnostics.json`: target/reason counts, score and
  margin summaries, exact-ISBN and Edition → Work anchors, operational errors,
  API network requests, cache hits, and the zero-Open-Library counter.
- `linking/output/sample/decisions.jsonl`: candidates, QIDs, generation path,
  evidence, score, conflicts, margin, decision and reason code.
- `linking/output/sample/errors.jsonl`: operational errors, separate from
  semantic `NO_MATCH`.
- `linking/output/wikidata_smoke/diagnostics.json`: regression observations.
- `linking/output/frozen_config.json`: v2 config with implementation, config,
  and 4-star input hashes.

A sample must finish without operational errors or anchor-check problems
before `freeze`. The full run rejects any changed code, config, or input.
Thresholds are conservative design parameters; they are not learned from
manual labels, and the experiment's coverage is not a precision claim.

## Read-only pre-full estimate

```bash
linking/.venv/bin/python linking/link_books.py estimate
# Optional: use another saved QID prerequisite snapshot
linking/.venv/bin/python linking/link_books.py estimate \
  --prerequisite-decisions /path/to/decisions.jsonl
```

The default snapshot is `linking/output/sample/decisions.jsonl`. This command
reads the local graph and saved accepted QIDs without changing decisions or
RDF. It reports distinct valid ISBNs, estimated `P212` batch count,
Language/Place text searches, and context eligibility under the snapshot.
The full Edition/Book stages can add prerequisites and raise later eligibility.
Actual HTTP traffic differs because of batching and cache hits.

## Full run (after audit)

Do not start this until the v2 bounded sample, smoke, diagnostics, and frozen
config have been reviewed. When authorized:

```bash
LINKING_CONTACT='your-contact@example.org' \
  linking/.venv/bin/python linking/link_books.py full
```

The run is sequential and resumable. It batches exact ISBN queries through
WDQS (25 ISBNs per query by default) and batches `wbgetentities` requests up
to 50 QIDs. Successful responses and decisions are cached. Operational errors
remain retryable and block final RDF materialization.

A complete run writes:

- `linking/output/full/external_links.ttl`: accepted Wikidata `owl:sameAs` links;
- `linking/output/full/books_5star.ttl`: original 4-star Turtle bytes plus links;
- `linking/output/full/decisions.jsonl`, `errors.jsonl`, `diagnostics.json`,
  and `output_manifest.json`.

The original 4-star Turtle is never overwritten. The prior Open Library-era
sample and frozen report are archived under
`linking/output/legacy_openlibrary_v1/` for audit only; v2 does not read them.
