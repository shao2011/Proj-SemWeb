# Notes on the pipeline changes

Hi! These are the changes on branch `fix/pipeline-quality` and the reason for each one. Numbers come from runs on `books_1.Best_Books_Ever.csv` (52,478 rows) on 2026-10-04 and 2026-10-05. Please review before we merge.

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

`example.org` can never be looked up, so it breaks LOD principle 1 (dereferenceable URIs, 04_LOD p.7) and the best practice of making vocabulary terms dereferenceable (04_LOD p.17). The movie project used `example.org` too.

Turning on GitHub Pages (Settings → Pages) is not enough by itself. Pages serves files at their own path and only adds `.html` implicitly, so `https://shao2011.github.io/Proj-SemWeb/ontology` needs a file `ontology.html` (or `ontology/index.html`), and every `/resource/...` URI needs a generated file of its own. Neither exists yet, so today all these URIs return 404.

Limitation for the report: Pages can't do content negotiation, so a browser and a SPARQL client get the same file.

## 4. The price column is gone

We dropped the CSV `price` column and `:price` from the ontology:

- It doesn't come from Goodreads. The Zenodo page says it was taken from Iberlibro, a second-hand bookshop.
- There's no currency, so prices can't be compared.
- It was a 2020 shop listing, so it describes a listing rather than the book.
- 27% of rows have no price and 12 are malformed (e.g. `1.189.88`).
- None of our competency questions use it.

## 5. Links to other datasets (5 stars)

`linking/link_books.py` writes `linking/output/links.ttl` (28,782 `owl:sameAs` triples) and `enrichment.ttl` (first-publication years). The output is committed, so you can build the endpoint without running the linker. Wikidata's public endpoint is slow: the run that added the ISBN lookups sent 563 new requests and took 26 minutes, the one that added the article title forms 441 requests and 15 minutes. Every answer is cached in `linking/.cache`, so a rerun takes 30 s with no network (`--offline` proves it).

| What | Linked | Out of |
|---|---|---|
| Books → Wikidata | 7,015 (70%) | 9,987 |
| Books → DBpedia | 5,723 | |
| Authors → Wikidata | 2,721 (64%) | 4,222 |
| Authors → DBpedia | 2,625 | |
| Series → Wikidata | 403 (16%) | 2,475 |
| Series → DBpedia | 266 | |
| Editions → Open Library | 8,113 (92%) | 8,802 with a valid ISBN |
| Editions → Wikidata | 1,633 (19%) | 8,802 with a valid ISBN |
| Publishers → Wikidata | 129 | 1,892 |
| Publishers → DBpedia | 98 | |
| Languages → Wikidata, DBpedia | 28 | 29 |

How a book gets its Wikidata item:

1. Wikidata already stores the Goodreads ID (P2969): 1,546 books. The Wikidata author must not contradict ours, because some of these IDs are wrong. Kaye Gibbons' "A Virtuous Woman" carried the ID of Zaynab Alkali's novel.
2. Otherwise the title must equal a label or alias, and one of our authors must equal the item's author (P50): 5,109 books. Another 248 only match after dropping a subtitle ("Nickel and Dimed: On (Not) Getting by in America"). The method column marks those as `short-title`. A leading "The", "A" or "An" may be added or dropped: Goodreads has "Murder at the Vicarage", Wikidata "The Murder at the Vicarage".
3. If the item has no P50, its English description must name our author ("1997 novel by J. K. Rowling"): 25 books. A P50 match always beats a description match. The play "Murder at the Vicarage" is "written by Agatha Christie" too, but only the novel has her as P50.
4. Books still unmatched go through the Wikidata search box, which is more forgiving about case, punctuation and aliases. The same checks apply.
5. Books still unmatched can go through one of their editions (next paragraph): the Wikidata edition's work (P629) must be a written work with our title and one of our authors. 5 books are linked this way, for example "Cathedral" and "Where I'm Calling From" by Raymond Carver. For 1,593 books both routes found a work, and they agree on all 1,593. Two independent methods giving the same answer is our best evidence that the title match is right.

The item must be a written work. Films, TV, radio and stage productions and fictional characters are rejected ("Richard Hannay" is typed both as a book series and as a fictional human), and so are series items, unless the class is a single-volume one such as graphic novel. If several candidates pass, the one sharing the most of our authors wins: "Nightfall" by Asimov and Silverberg is the 1990 novel, not Asimov's 1941 story. If that still leaves several, the book is linked only when one has at least 3 Wikipedia sitelinks and twice as many as the next (83 books), or else when exactly one has our title without an article change (1 book, "A Court of Mist and Fury", against a stray item "Court of Mist and Fury"). Otherwise it stays unlinked (78 ambiguous). A Wikidata item gets at most one of our books (24 collisions dropped). When two books want one item, a book whose ISBN route confirms the match wins: "Midnight Sun [2008 Draft]" and the 2020 "Midnight Sun" both reach Q1052268 through Goodreads IDs stored in Wikidata, and only the 2020 book through its ISBN. 2,870 books had no candidate at all.

Wikidata sometimes has two items for one novel. "The Architect's Apprentice" now links to Q123357537, which has Elif Şafak as P50, instead of Q19612908, which has only a description and a Wikipedia article. Both are the same novel, but the book lost its DBpedia link.

We don't search for authors and series. Each one takes the author (P50) or series (P179) item of its linked books whose label matches its name, by majority vote and one-to-one. A series item must also be classified as a series in Wikidata. DBpedia IRIs come from each item's English Wikipedia article, so we never query DBpedia. Its public endpoint gave us 500 errors and timeouts.

ISBNs are checked first. 26 fail the ISBN-10/13 checksum or are Barnes & Noble ebook numbers (prefix 294) and placeholders like `9999999999999`; they are never looked up. ISBN-10s are converted to ISBN-13.

Editions link to Open Library by ISBN when the titles agree. The rule skips 344 title mismatches. Volume numbers must agree, which caught "Akira, Vol. 1" → "Akira, Vol. 4". It also drops 11 links where two of our editions point at one Open Library record. That happens when Open Library merges volumes (Transmetropolitan 1-3 are one record) or Goodreads lists a book twice with the same ISBN (Wolf Hall). An edition `isEditionOf` exactly one Book, so `owl:sameAs` there would merge different books.

Editions link to Wikidata when an item carries the ISBN as P212 (ISBN-13) or P957 (ISBN-10). Wikidata stores ISBNs hyphenated, so we query every hyphenation, 25 ISBNs per request. 1,693 ISBNs are found. The item must be an edition (not the work, which would make the edition `sameAs` the book) and have our title: that rejects 31 non-editions and 25 title mismatches. The same one-target-per-edition rule as for Open Library drops 2 more. This and the publisher links follow Hao Nguyen's Wikidata linker on `main`. His output was also a useful cross-check: on the resources we both link, we picked the same Wikidata item for 1,589 of 1,590 books, 1,058 of 1,058 authors, 196 of 196 series and 1,616 of 1,616 editions (`HANDOFF.md` has the full comparison).

Publishers take the publisher (P123) of their linked Wikidata editions, when the names agree after ignoring words like "Books", "Inc." and "Publishing" ("Avon" = "Avon Books"). "Press" and "Group" count, because "Scholastic Press" is an imprint of Scholastic and "Penguin Group" is the parent of Penguin Books. Each Wikidata item gets one publisher of ours: our data has "Ace", "Ace Book" and "Ace Books" as three publishers, and only the one with the most editions is linked (33 dropped). 129 of 1,892 are linked. Most publishers have no linked edition to go through.

Languages are matched by name among Wikidata items with an ISO 639-2 code (P219), so "English" can't land on a film or a novel called "English". 28 of 29 are linked. "Multiple languages" is the one left.

First-publication year: 5,933 books get `firstPublishYear` from Wikidata P577 (earliest date). It is never later than an edition year we already have, which rejected 77. Where the CSV did have a year, Wikidata agrees on 70 and disagrees on 7. Coverage went from 133 books to 6,066 (61%).

### Precision

`linking/output/review_sample.csv` is a fixed sample (seed 42): 100 random books, up to 25 more from each weaker method (short title, description, sitelinks, ISBN edition), 40 authors, 20 series, 40 Open Library editions, 40 Wikidata editions, 30 publishers and 28 languages. All 377 rows were checked by hand, and none is wrong now. Fixing what the checks found produced several of the rules above. Each has a test in `linking/test_linking.py`:

- "A Court of Thorns and Roses" was linked to the series item, "Last Chance to See" to the radio documentary, and "Dear Evan Hansen" to the musical.
- "The Morganville Vampires, Volume 1" was linked to Open Library's Volume 2 record. The volume-number rule then removed two more of the same kind (Akira, Fruits Basket).
- "Murder at the Vicarage" was linked to the stage play, because only the play's label lacks "The". Article forms plus "P50 beats description" fixed it. Adding article forms first broke "A Court of Mist and Fury" (a stray item without the article) and pulled "The Aeneid" to a stray item labelled "The Aeneid". Hence the order: shared authors, then sitelinks, then the exact title.
- "Nightfall" (Asimov and Silverberg) was linked to the 1941 short story, which has more sitelinks than the novel.
- "Midnight Sun" (2020) was unlinked because "Midnight Sun [2008 Draft]", with more ratings, took Q1052268. Found by comparing with Hao's output, which had it right.
- Plato's "Apology" got the year `-395`, which is not a valid `xsd:gYear`. It is now `-0395`.

Unmatched and rejected cases, with reasons, are in `linking/output/link_issues.json`.

## 6. Reasoning

- `reasoning/hermit_check.sh` runs HermiT 1.4.3 (the one bundled with Protégé) without the GUI. The ontology plus the data (571,013 triples) is consistent, which took 22 s and 1.7 GB. It infers Author, Translator, Illustrator, Editor and Narrator under Person, and AudiobookEdition, TranslatedEdition and IllustratedEdition under BookEdition.
- `reasoning/materialize.py` computes the OWL 2 RL closure with `owlrl` and keeps only the new triples: 312,880 of them. Fuseki doesn't reason by default (Ext_SPARQL p.42-45), so we load these as their own graph. The `owl:sameAs` links are left out on purpose. With them, OWL-RL would copy every fact about a linked book onto its Wikidata and DBpedia IRIs.

## 7. SPARQL endpoint

- `endpoint/load.sh` builds a TDB2 database with one named graph per source: ontology, data, inferred, links, enrichment and VoID metadata. That's 918,643 triples, loaded in 18 s.
- `endpoint/run_fuseki.sh` starts Fuseki 6.2.0 at `http://localhost:3030/books/sparql`. It is read-only: updates and Graph Store writes return HTTP 405.
- `endpoint/run_queries.py` runs the 11 competency questions in `endpoint/queries/`, and every one returns rows. CQ1 needs the inferred `writes`. CQ9 counts the links per kind and dataset. CQ11 is federated: it follows our `owl:sameAs` to Wikidata with `SERVICE` to get the authors' birth places, which takes a few seconds.

## Known gaps

- 3,921 books (39%) still have no first-publication year. Wikidata has no P577 for them or they are unlinked, for example most Sandman volumes.
- 2,972 books (30%) have no Wikidata link.
- The URIs don't resolve yet (section 3): GitHub Pages is off and no files are generated for `/ontology` or `/resource/...`. `void:sparqlEndpoint` is `localhost`, so the endpoint is only reachable while we run it.

## Checks

- `pipeline/test_pipeline.py` (29) and `linking/test_linking.py` (49): all 78 tests pass. The new pipeline tests fail on the old converter.
- `riot --validate ontology.ttl` passes, and `endpoint/load.sh` loads every file without warnings.
- `verify_output.py` passes on both the 10k run and the full run.
