# Handoff for Hao

This branch (`top10k`) holds the whole project except the report, the slides and the video. Please read this file, review the PR, and then publish the site (section 4: a few commands and one settings change). `NOTES.md` explains each design decision in more detail. `README.md` lists the commands.

## 1. What is in this branch

| Commit | What |
|---|---|
| `4c8d794` | Pipeline fixes: author role aliases, year properties, roles derived by the reasoner, audio formats, disjointness |
| `8ba28c6` | Top-10k selection, `shao2011.github.io` namespace, price column dropped, `NOTES.md` |
| `631c4ba` | Linker (Wikidata, DBpedia, Open Library), reasoning scripts, Fuseki endpoint with 11 queries |
| `c4858da` | Your ideas ported: ISBN checksums, Wikidata editions by ISBN, publishers through editions, languages |
| `ecb92bf` | Titles with or without "The/A/An", stricter tie-breaks, review sample re-checked |
| `3da2d09` | Merge of your `main`, so the PR has no conflicts (section 3) |
| `4bf83ba` | Midnight Sun fix found through your output, this file |
| `36319b5` | Data dumps as the GitHub release `data-v1`, `void:dataDump` points at them |
| last | `publish/build_pages.py`: static site that makes every URI resolve (section 4) |

## 2. Your work compared with ours

Measured on 2026-10-06. Your numbers come from `external_links.ttl` on the Drive. Counts are Wikidata links, for the 10,000 books in our dataset unless marked "total".

| | Yours, total | Yours, within the top 10k | Ours | Both linked | Same Wikidata item |
|---|---:|---:|---:|---:|---:|
| Books | 3,228 of 51,775 (6%) | 1,595 | **7,015 (70%)** | 1,590 | 1,589 |
| Authors | 1,770 | 1,107 | **2,721** | 1,058 | 1,058 |
| Series | 287 | 213 | **403** | 196 | 196 |
| Editions | 3,423 | 1,641 | 1,633 | 1,616 | 1,616 |
| Publishers | 197 | 126 | 129 | 53 | 53 |
| Languages | 49 | 20 | 28 | 20 | 20 |
| Places | 55 | 35 | 0 | – | – |

Where we both link something, we almost always picked the same item. That's the best evidence we have that both linkers are accurate, and it costs nothing to show in the presentation. The one disagreement is "Fantastic Voyage": you link the 1966 film, we link Asimov's novel.

### What we took from your linker

- ISBN-10/13 checksum validation, so a typo or a placeholder like `9999999999999` is never looked up.
- Wikidata editions by ISBN (P212/P957, every hyphenation, 25 ISBNs per query). Each edition gets at most one target and each target one edition.
- Edition → work (P629) as a second route to the book. For the 1,593 books where both routes found a work, they agree on all 1,593.
- Publishers through the P123 of linked editions, and languages.
- Your output as a check. It found "Midnight Sun": our 2020 book had lost Q1052268 to "Midnight Sun [2008 Draft]". The fix: a book confirmed by its ISBN edition now wins such a collision.

### Why we think this branch should be the base

1. **Coverage.** Your linker reaches a book only through an edition that Wikidata already has, so it links 6% of the 52k books. We match title + author first (with the checks in `NOTES.md` section 5) and use editions as the second route, so 70% of the top 10k are linked.
2. **Nothing gets merged by mistake.** In your output, 33 Wikidata items are each linked from several of your resources. `owl:sameAs` turns those into one individual:
   - Plutarch's "Lives" Volume I and Volume II become one book.
   - Dav Pilkey, George Beard and Harold Hutchins become one person. The last two are fictional characters from Captain Underpants.
   - "Crown Publishers", "Crown Publishing Group" and "Crown Publishing Group NY" become one publisher. The same happens to Putnam, Norton, Houghton Mifflin and Orion.
   - Some imprints are linked to their parent company: Minotaur Books → Macmillan Publishers, Harper Business → Harper.

   Ours uses one-to-one links everywhere. Your one-target-per-edition rule is now part of it.
3. **The data under the links.** Your Drive files come from the original converter:
   - `example.org` URIs.
   - 579 rows dropped because roles like "(Writer)" were not mapped.
   - First-publication dates for only 1,157 books.
   - The price column (37,603 values, taken from a second-hand shop, without a currency).

   This branch fixes all of that. 6,066 of the top 10k books now have a first-publication year, most of them from Wikidata.
4. **Five stars.** Ours links to three datasets: Wikidata, DBpedia (through Wikipedia sitelinks) and Open Library (8,113 editions). Yours links to Wikidata only.
5. **Reasoning.** HermiT finds the ontology plus the data consistent. The OWL-RL inferences (312,880 triples) are loaded into the endpoint. Your endpoint has no reasoning step.
6. **The endpoint.** Your `run_fuseki.sh` has paths from your machine (`/home3/haons/...`) and loads a 203 MB Turtle file into memory. Ours builds a TDB2 database with six named graphs (data, inferred, links, ...), runs read-only, and comes with 11 competency queries plus a runner. CQ11 is federated: it follows our `owl:sameAs` links to Wikidata live.
7. **Checked by hand.** All 377 rows of a fixed review sample are correct. There are 78 tests. Every rule in `linking/link_books.py` comes from a real error we found, and each has a test.

### Where your linker is still ahead

- **Places:** 35 links. We link none.
- **Publishers:** your name search reaches about 50 correct publishers we never reach, for example Gallimard, Routledge and Oxford University Press. Ours only go through linked editions.

Section 5 has both as optional tasks.

## 3. What this PR does to your files

`3da2d09` merges your `main`. In the three files that conflicted (`README.md`, `linking/link_books.py`, `linking/test_linking.py`) we kept this branch's version.

It also removes `linking/core.py`, `external.py`, `matcher.py`, `config.json`, `experiment_wikidata_isbn.py`, `linking/README.md`, `linking/requirements.txt` and `sparql-endpoint/`. Without them the repo would have two linkers and two endpoints, and `linking/README.md` would describe code that's no longer there. Nothing is lost: all of it stays in `main`'s history at `78581b9`. Your Drive files are not touched.

If you'd rather keep a piece of it, say so in the PR and we'll put it back.

## 4. Remaining task for you: publish the site (GitHub Pages)

LOD principle 1 says a URI should return something useful when you look it up (04_LOD p.7, best practices p.17). The repo is public and Pages is on, but Pages builds from `top10k` and only shows the README: `/ontology` and every `/resource/...` URI return 404, because no HTML file exists for them. `publish/build_pages.py` now generates those files. What's left needs you, because changing the Pages source needs admin rights on the repo.

### 4.1 What the script builds

`pipeline/.venv/bin/python publish/build_pages.py` reads the five files of the endpoint's named graphs (data, inferred, links, enrichment, VoID) plus `ontology.ttl`, and writes `site/` (gitignored) in about 50 s:

- `site/resource/<kind>/<slug--hash>.html` for each of the 76,971 resources, plus `dataset.html` and `linkset/*.html` for the VoID URIs. Pages serves `x.html` for a request to `/x`, so `https://shao2011.github.io/Proj-SemWeb/resource/book/the-hunger-games--4966d945b5bd060f` gets its page. Each page has a table of the resource's triples from all four graphs (inferred ones in italics), labels instead of raw URIs, relative links to our other resources, external links for `owl:sameAs` targets, a "Referenced by" list (for example the books of a person), and a `DESCRIBE` link to the SPARQL endpoint.
- The same triples as JSON-LD in `<script type="application/ld+json">` on every page, because Pages can't do content negotiation.
- `ontology.html`: one section per class and property with `id="Book"` etc., so `.../ontology#Book` lands on its section. It shows labels, comments, super-classes, equivalent classes (OWL restrictions in Manchester syntax), disjointness, domains, ranges and inverses. `ontology.ttl` is copied next to it.
- `index.html` (links to the ontology, `void.ttl`, the release dumps, the repo, an example per kind and the 10 most-rated books), `404.html`, `style.css` and `.nojekyll`.

Measured: 76,982 files, 336 MiB of content (484 MB on disk), well under the 1 GB Pages limit; a commit of it packs to 125 MiB. The JSON-LD of 304 sampled pages parses back to exactly the triples on the page, and the ontology's JSON-LD is isomorphic to `ontology.ttl`. `publish/test_build_pages.py` checks the URI → file mapping and that every link resolves to its target URI from any page.

Preview it locally first, the way Pages serves it (`/x` → `x.html`):

```bash
pipeline/.venv/bin/python publish/build_pages.py --serve 8000   # http://localhost:8000/
```

### 4.2 Push `site/` as the orphan branch `gh-pages`

77k generated files don't belong on `main`. From the repo root, with `pipeline/output/books_data.ttl` and `reasoning/output/inferred.ttl` present (build them or take them from the release, see README):

```bash
SHA=$(git rev-parse --short HEAD)
pipeline/.venv/bin/python publish/build_pages.py
TMP=$(mktemp -d)                                   # git metadata outside site/, so the next build can delete site/
git --git-dir="$TMP" --work-tree=site init -q -b gh-pages
git --git-dir="$TMP" --work-tree=site add -A
git --git-dir="$TMP" --work-tree=site commit -qm "Site built from $SHA"
git --git-dir="$TMP" --work-tree=site push -f https://github.com/shao2011/Proj-SemWeb.git gh-pages
rm -rf "$TMP"
```

Each run replaces `gh-pages` with a single commit, so the branch never accumulates old versions.

### 4.3 Switch the Pages source (admin only)

Settings → Pages → Build and deployment → Source "Deploy from a branch" → branch `gh-pages`, folder `/ (root)` → Save. Or:

```bash
gh api -X PUT repos/shao2011/Proj-SemWeb/pages -f 'source[branch]=gh-pages' -f 'source[path]=/'
```

Today the source is `top10k`. That branch goes away when the PR is merged, and the site would break with it.

### 4.4 Done when

```bash
for p in "" ontology resource/book/the-hunger-games--4966d945b5bd060f "resource/book/the-good-soldier-švejk--0220c05aa8cc81eb"; do
  curl -s -o /dev/null -w "%{http_code} /$p\n" "https://shao2011.github.io/Proj-SemWeb/$p"; done   # all 200
```

The first deployment of 77k files can take several minutes; the Actions tab shows its progress. Then the Hunger Games page should show its author, editions and links to Wikidata and DBpedia. Afterwards, update `NOTES.md` section 3 and "Known gaps" (they say the site is not published yet).

## 5. Optional, only if there is time

- **Publishers by name:** port your publisher name search into the publisher step of `run()` in `linking/link_books.py` (the block starting `# Publishers:`). Keep our rules: the name must agree under `same_publisher` (which drops words like "Books" and "Inc."), an imprint must not be linked to its parent, and the result goes through `one_to_one`. That's about 50 more correct publisher links.
- **Places:** port your place matching (35 links), also through `one_to_one`.

Each needs a test in `linking/test_linking.py` and a hand-checked sample in `review_sample.csv`.

## 6. How to check the PR

```bash
uv venv pipeline/.venv && uv pip install --python pipeline/.venv/bin/python -r pipeline/requirements.txt
PY=pipeline/.venv/bin/python
$PY -m pytest -q pipeline/test_pipeline.py linking/test_linking.py      # 78 passed
$PY pipeline/books_pipeline.py && $PY reasoning/materialize.py         # needs books_1.Best_Books_Ever.csv in the repo root
endpoint/load.sh && endpoint/run_fuseki.sh                             # needs Jena + Fuseki 6 on PATH
$PY endpoint/run_queries.py                                            # 11 queries, all return rows
```

You don't need to run the linker, because `linking/output/` is committed. A cold run takes a long time: Wikidata's public endpoint answers each batch in seconds, and the last two additions alone took 26 and 15 minutes.
