#!/usr/bin/env bash
# Start the read-only SPARQL endpoint: http://localhost:3030/books/sparql
# (UI at http://localhost:3030/). Build the database first with endpoint/load.sh.
# Needs Fuseki (fuseki-server) on PATH.
set -euo pipefail
cd "$(dirname "$0")"
[ -d db ] || { echo "no database: run endpoint/load.sh first" >&2; exit 1; }
export FUSEKI_BASE="$PWD/run"
exec fuseki-server --config=fuseki-config.ttl "$@"
