#!/usr/bin/env bash
# Build the TDB2 database for Fuseki: one named graph per source, so asserted,
# inferred and linked facts stay distinguishable.
# Run after: pipeline/books_pipeline.py, reasoning/materialize.py, linking/link_books.py
# Needs Apache Jena (tdb2.tdbloader) on PATH.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DB="$ROOT/endpoint/db"
G="https://shao2011.github.io/Proj-SemWeb/graph"

declare -a SOURCES=(
  "ontology|$ROOT/ontology.ttl"
  "data|$ROOT/pipeline/output/books_data.ttl"
  "inferred|$ROOT/reasoning/output/inferred.ttl"
  "links|$ROOT/linking/output/links.ttl"
  "enrichment|$ROOT/linking/output/enrichment.ttl"
  "metadata|$ROOT/endpoint/void.ttl"
)
for entry in "${SOURCES[@]}"; do
  [ -f "${entry#*|}" ] || { echo "missing ${entry#*|}" >&2; exit 1; }
done

rm -rf "$DB"
for entry in "${SOURCES[@]}"; do
  name="${entry%%|*}"; file="${entry#*|}"
  echo "== <$G/$name>  <-  ${file#$ROOT/}"
  tdb2.tdbloader --loc "$DB" --graph "$G/$name" "$file" 2>&1 | grep -E 'Time =|ERROR|WARN' || true
done
echo "== triples per graph"
tdb2.tdbquery --loc "$DB" \
  'SELECT ?g (COUNT(*) AS ?triples) WHERE { GRAPH ?g { ?s ?p ?o } } GROUP BY ?g ORDER BY ?g'
