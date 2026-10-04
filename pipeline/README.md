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
`--resource-base`, `--two-digit-year-pivot`, and `--top-n`. By default the Turtle is
`pipeline/output/books_data.ttl` and the QA files are in `pipeline/output/qa/`.
The fixed default pivot is `26`, representing the 2026 handover context; an
edition date `09/14/08` becomes `2008-09-14` and is counted in QA.

`--top-n` (default `10000`) keeps the N `bookId`s with the most `numRatings`
(ties: smaller `bookId`), including every duplicate row of those IDs;
`--top-n 0` converts all rows. Resource URIs default to
`https://shao2011.github.io/Proj-SemWeb/resource/`. The CSV `price` column is
not converted. See `../NOTES.md` for why.

## Processing

1. Parse each row into validated fields. Invalid optional values produce QA
   records; rows lacking `bookId`, title, or any contributor are skipped.
   Goodreads role annotations are split on `,` `/` `&` `and` and mapped through
   `ROLE_ALIASES` (e.g. Writer, Creator, co-author → author; Artist →
   illustrator; Übersetzer, ترجمة → translator). Rows whose contributors are all
   non-authors (anthologies, scriptures, adaptations) are kept without
   `isWrittenBy`, grouped by their first contributor, and listed in
   `works_without_author`.
2. Resolve editions by trimmed `bookId`; conflicting duplicate IDs are
   reported, and one deterministic work assignment is selected.
3. Group editions using `canonical(title)|canonical(primary_author)`. Choose
   the representative row by `numRatings`, `bbeVotes`, then smallest `bookId`.
   Union authors, categories, characters, places, awards, and series; choose
   the earliest reliably parsed `firstPublishDate` and earliest `firstPublishYear`.
4. Emit asserted source facts with RDFLib. Generated resource URIs use a
   canonical key plus SHA-256. Audio formats (Audio CD, Audiobook, Audible
   Audio, MP3 CD, ...) are typed `AudioFormat` so `AudiobookEdition` is derived.
5. Write one JSON summary and twelve detailed issue files. The verifier
   reparses the Turtle and checks counts, domains, literals, links, and
   ontology vocabulary. It prints an order independent graph fingerprint
   for the **reparsed Turtle**; compare fingerprints from reparsed files.

No inverse properties, external superproperties, or derived class types
(Author, Translator, Illustrator, Editor, Narrator, AudiobookEdition, ...) are
materialized. The reasoning fixture combines data with `ontology.ttl` and
checks the expected inferences using OWL-RL.

## Conservative fallbacks

- Two-digit `firstPublishDate` values are omitted with `ambiguous_century`
  (the source CSV lost the century; recover the year from Wikidata at linking).
- Year-only or month/year source dates produce no `xsd:date` (that would need
  an invented day) but do produce `publishYear` / `firstPublishYear`
  (`xsd:gYear`). The raw value is retained in QA.
- Complex series positions retain the safe series name, omit the position,
  and generate an `invalid_values` record.
- Awards without a reliable terminal four-digit year are omitted; known
  non-winning statuses outside the ontology's three specific subclasses use
  generic `AwardRecognition` and generate an issue.
- Unsupported contributor annotations become `hasEditionContributor` and are
  reported in `unknown_contributor_roles` (one record per unmapped token).
