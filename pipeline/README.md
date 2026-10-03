# Books CSV → RDF pipeline

This directory converts `books_1.Best_Books_Ever.csv` into an instance graph for
the existing `ontology.ttl`. The ontology is loaded for vocabulary checks and
is never modified or copied into the generated Turtle.

## Run

```bash
uv venv pipeline/.venv
uv pip install --python pipeline/.venv/bin/python -r pipeline/requirements.txt
pipeline/.venv/bin/python pipeline/books_pipeline.py
pipeline/.venv/bin/python -m pytest -q pipeline/test_pipeline.py
pipeline/.venv/bin/python pipeline/verify_output.py
```

The CLI accepts `--input`, `--ontology`, `--output`, `--qa-dir`,
`--resource-base`, and `--two-digit-year-pivot`. By default the Turtle is
`pipeline/output/books_data.ttl` and the QA files are in `pipeline/output/qa/`.
The fixed default pivot is `26`, representing the 2026 handover context; an
edition date `09/14/08` becomes `2008-09-14` and is counted in QA.

## Processing

1. Parse each row into validated fields. Invalid optional values produce QA
   records; rows lacking `bookId`, title, or primary author are skipped.
2. Resolve editions by trimmed `bookId`; conflicting duplicate IDs are
   reported, and one deterministic work assignment is selected.
3. Group editions using `canonical(title)|canonical(primary_author)`. Choose
   the representative row by `numRatings`, `bbeVotes`, then smallest `bookId`.
   Union authors, categories, characters, places, awards, and series; choose
   the earliest reliably parsed `firstPublishDate`.
4. Emit asserted source facts with RDFLib. Generated resource URIs use a
   canonical key plus SHA-256. The ontology's existing `:Audiobook` individual
   is reused so its `AudiobookEdition` reasoning axiom works.
5. Write one JSON summary and eleven detailed issue files. The verifier
   reparses the Turtle and checks counts, domains, literals, links, and
   ontology vocabulary. It prints an order independent graph fingerprint
   for the **reparsed Turtle**; compare fingerprints from reparsed files.

No inverse properties, external superproperties, or derived class types are
materialized. The reasoning fixture combines data with `ontology.ttl` and
checks the expected inferences using OWL-RL.

## Conservative fallbacks

- Two-digit `firstPublishDate` values are omitted with `ambiguous_century`.
- Year-only or month/year source dates are omitted because an `xsd:date`
  would require inventing a day. The raw value is retained in QA.
- Complex series positions retain the safe series name, omit the position,
  and generate an `invalid_values` record.
- Awards without a reliable terminal four-digit year are omitted; known
  non-winning statuses outside the ontology's three specific subclasses use
  generic `AwardRecognition` and generate an issue.
- Unsupported contributor annotations become `hasEditionContributor` and are
  reported. If no contributor qualifies as an author, the row is skipped.
