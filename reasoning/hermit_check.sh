#!/usr/bin/env bash
# Run HermiT (the reasoner bundled with Protégé 5.6.9) from the command line:
#   1. consistency + unsatisfiable classes of ontology.ttl
#   2. inferred sub-classes of Person, Book and BookEdition (the defined classes)
#   3. consistency of ontology.ttl + the generated instance data
#
# Usage: reasoning/hermit_check.sh [data.ttl]   (default pipeline/output/books_data.ttl)
# Needs: java, riot (Apache Jena), PROTEGE_HOME pointing at Protégé 5.6.9.
#
# HermiT only supports the OWL 2 datatype map; xsd:date and xsd:gYear are not in it.
# Protégé's HermiT plugin sets ignoreUnsupportedDatatypes, so we pass the same flag.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA="${1:-$ROOT/pipeline/output/books_data.ttl}"
: "${PROTEGE_HOME:?set PROTEGE_HOME to the Protégé 5.6.9 directory}"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
LIB="${HERMIT_LIB:-$ROOT/reasoning/.hermit-lib}"

if [ ! -f "$LIB/java-getopt-1.0.13.jar" ]; then   # unpack the jars Protégé ships inside its bundles
  mkdir -p "$LIB"
  unzip -q -o "$PROTEGE_HOME/bundles/owlapi-osgidistribution.jar" 'lib/*.jar' -d "$WORK/o"
  unzip -q -o "$PROTEGE_HOME/plugins/org.semanticweb.hermit-1.4.3.456.jar" '*.jar' -d "$WORK/h"
  find "$WORK/o" "$WORK/h" -name '*.jar' -exec cp {} "$LIB/" \;
  curl -sSfL -o "$LIB/java-getopt-1.0.13.jar" \
    https://repo1.maven.org/maven2/gnu/getopt/java-getopt/1.0.13/java-getopt-1.0.13.jar
fi
B="$PROTEGE_HOME/bundles"
CP="$PROTEGE_HOME/plugins/org.semanticweb.hermit-1.4.3.456.jar:$B/owlapi-osgidistribution.jar:$B/guava.jar"
CP="$CP:$B/slf4j-api.jar:$B/jsr305.jar:$B/commons-io.jar:$B/org.apache.servicemix.bundles.javax-inject.jar:$LIB/*"
hermit() { java "${HERMIT_JAVA_OPTS:--Xmx4g}" -cp "$CP" org.semanticweb.HermiT.cli.CommandLine \
             --ignoreUnsupportedDatatypes "$@" 2>&1 | grep -v '^SLF4J'; }

echo "== 1. ontology: consistency and unsatisfiable classes"
hermit -k -U "file://$ROOT/ontology.ttl"
echo "== 2. inferred sub-classes"
hermit -s ':Person' -s ':Book' -s ':BookEdition' "file://$ROOT/ontology.ttl"
echo "== 3. ontology + $(basename "$DATA"): consistency"
riot --formatted=ntriples "$ROOT/ontology.ttl" "$DATA" > "$WORK/merged.nt" 2>/dev/null
echo "   $(wc -l < "$WORK/merged.nt") triples"
start=$SECONDS
hermit -k "file://$WORK/merged.nt"
echo "   ($((SECONDS - start)) s)"
