# Notes on the pipeline changes

Hi! These are the changes on branch `fix/pipeline-quality` and the reason for each one. Numbers come from runs on `books_1.Best_Books_Ever.csv` (52,478 rows) on 2026-10-04. Please review before we merge.

## 1. Bugs fixed in the converter

- 579 books were dropped because the author had a role like "(Writer)", "(Creator)" or "(co-author)". These now count as authors, "(Artist)" counts as illustrator, and combined roles like "(Writer, Artist)" are split. Anthologies and scriptures with no listed author (341 in the full CSV) are kept without `isWrittenBy` and listed in `qa/works_without_author.json`. Nothing is skipped now.
- Year-only dates were thrown away. We added `publishYear` and `firstPublishYear` (`xsd:gYear`) so a date like "1997" is kept without inventing a day.
- Illustrator, Editor and Narrator are now derived by the reasoner, the same way Translator already was. Award win and nomination are inverses. Every audio format (Audio CD, Audible Audio, MP3 CD, ...) makes an `AudiobookEdition`. Award win, nomination and finalist are disjoint, and so are Person and Organization.
- On the top 1,000 books, OWL-RL derived 613 Authors, 95 Translators, 57 Illustrators, 21 Editors and 10 Narrators, with nothing in `owl:Nothing`.

## 2. We convert the top 10,000 books, not all 52k

The pipeline now keeps the 10,000 `bookId`s with the most ratings (`--top-n`, default 10000). `--top-n 0` still converts everything.

Why:

- The rule is objective and anyone can rerun it. Book #10,000 has 13,776 ratings; the median book in the CSV has 2,311. These 10k books get 87.5% of all ratings.
- They have much more data to model. Awards are filled for 45% of them against 20% overall, characters for 62% against 26%, and setting for 49% against 22%.
- They link better to Wikidata, which we need for 5 stars. Exact title + author matches: 241/300 in the top 300, 136/300 for ranks 5,001-10,000, 85/300 for random books from the whole CSV.
- Reasoning gets heavy. OWL-RL on 1,000 books took 62 s and grew the graph from 82k to 213k triples. The full graph is 2.24M triples, too big for Protégé, and the converter needed 2.8 GB of RAM (it was OOM-killed once on a 16 GB laptop with a browser open).
- It matches the movie project, which also had 10,000 movies.

The 10k run takes 28 s and 750 MB: 9,987 books, 10,000 editions, 570,639 triples. The verifier passes.

What we lose: the top 10k is 96% English, against 81% in the full CSV. We'll say so in the report, together with the full-data numbers.

## 3. URIs moved from example.org to GitHub Pages

Ontology terms are now `https://shao2011.github.io/Proj-SemWeb/ontology#...` and resources are `https://shao2011.github.io/Proj-SemWeb/resource/...`.

`example.org` can never be looked up, so it breaks LOD principle 1 (dereferenceable URIs, 04_LOD p.7) and the best practice of making vocabulary terms dereferenceable (04_LOD p.17). The movie project used `example.org` too. With Pages, `ontology.ttl` is served at the ontology URL, and each book or author can get a static page with JSON-LD in it.

You need to do this part: turn on GitHub Pages for this repo (Settings → Pages, branch `main`). Until then the URIs are well-formed but don't resolve yet.

Limitation for the report: Pages can't do content negotiation, so a browser and a SPARQL client get the same file.

## 4. The price column is gone

We dropped the CSV `price` column and `:price` from the ontology:

- It doesn't come from Goodreads. The Zenodo page says it was taken from Iberlibro, a second-hand bookshop.
- There's no currency, so prices can't be compared.
- It was a 2020 shop listing, so it describes a listing rather than the book.
- 27% of rows have no price and 12 are malformed (e.g. `1.189.88`).
- None of our competency questions use it.

## Known gap

Only 133 of the top 10k books get a `firstPublishYear`. The CSV wrote the first 30,000 rows' dates as `mm/dd/yy`, so `01/28/13` could be 1813 or 1913, and the original scraper doesn't keep the century either. We plan to fill this from Wikidata (P577) during linking.

## Checks

- `pipeline/test_pipeline.py`: 29 tests pass. The new ones fail on the old code.
- `riot --validate ontology.ttl` passes.
- `verify_output.py` passes on both the 10k run and the full run.
