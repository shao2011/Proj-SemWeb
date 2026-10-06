# Proj-SemWeb
Link download data (1 file CSV): https://zenodo.org/records/4265096

Yêu cầu của thầy:
1. Define an ontology for the selected domain -> output là file `ontology.ttl`
2. Collect relevant data in this domain -> là bộ data CSV trên zenodo
3. Transform collected data into 4* standard -> Code trong dir `pipeline`, tự chạy lại sẽ ra file `book_data.ttl` (require: file data CSV + file `ontology.ttl`)
4. Find and establish links to other datasets to obtain 5* standard -> dir `linking` (Wikidata, DBpedia, Open Library)
5. Provide an interface via SPARQL endpoint/termnal to query data -> dir `endpoint` (Fuseki + 11 competency-question queries)

Reasoning (HermiT check + OWL-RL inferences) is in `reasoning`. Why things are done this way: `NOTES.md`.

## Run everything

Tools: Python 3.12 with `pipeline/requirements.txt`, Java 21, Apache Jena + Fuseki 6.x
(`riot`, `tdb2.tdbloader`, `fuseki-server` on `PATH`), Protégé 5.6.9 (`PROTEGE_HOME`) for HermiT.
Put `books_1.Best_Books_Ever.csv` in the repo root.

```bash
uv venv pipeline/.venv && uv pip install --python pipeline/.venv/bin/python -r pipeline/requirements.txt
PY=pipeline/.venv/bin/python

$PY pipeline/books_pipeline.py          # 3. CSV -> pipeline/output/books_data.ttl (top 10,000 books, ~30 s)
$PY pipeline/verify_output.py           #    checks the Turtle against the CSV and QA files
reasoning/hermit_check.sh               #    HermiT: ontology + data consistent? (~25 s)
$PY reasoning/materialize.py            #    OWL-RL -> reasoning/output/inferred.ttl (~8 min, 1.6 GB RAM)
$PY linking/link_books.py               # 4. links -> linking/output/ (~5,400 HTTP requests, cached in linking/.cache/;
                                        #    cached rerun ~1 min; --offline never touches the network)
endpoint/load.sh                        # 5. TDB2 database, one named graph per source (~20 s)
endpoint/run_fuseki.sh                  #    http://localhost:3030/books/sparql  (UI: http://localhost:3030/)
$PY endpoint/run_queries.py             #    runs endpoint/queries/*.rq, saves CSVs in endpoint/results/

$PY -m pytest -q pipeline/test_pipeline.py linking/test_linking.py
```

`linking/output/` is committed, so steps 5 onwards work without running the linker.

## Layout

| Path | What |
|---|---|
| `ontology.ttl` | OWL 2 ontology (Work/Edition split, contributor roles, n-ary award recognitions, defined classes) |
| `pipeline/` | CSV -> RDF converter, verifier, tests, QA reports |
| `reasoning/hermit_check.sh` | HermiT 1.4.3 (from Protégé) on the command line: consistency, unsatisfiable classes, inferred hierarchy |
| `reasoning/materialize.py` | OWL 2 RL closure with `owlrl`; writes only the new triples |
| `linking/link_books.py` | `owl:sameAs` for books, authors, series, editions, publishers and languages to Wikidata / DBpedia / Open Library, first-publication years from Wikidata, review sample |
| `endpoint/` | Fuseki config, loader, VoID metadata, competency-question queries and runner |

## Named graphs in the endpoint

The default graph is the union of all of them, so queries need no `GRAPH` clause.

| Graph (`https://shao2011.github.io/Proj-SemWeb/graph/...`) | Content |
|---|---|
| `ontology` | `ontology.ttl` |
| `data` | facts from the CSV |
| `inferred` | OWL-RL inferences |
| `links` | `owl:sameAs` links |
| `enrichment` | `firstPublishYear` from Wikidata |
| `metadata` | VoID description (licence, provenance, link sets) |
