#!/usr/bin/env python3
"""Materialize OWL 2 RL inferences over ontology.ttl + the instance data.

Writes only the *new* triples (closure minus input) so Fuseki can keep asserted and
inferred facts in separate named graphs. owl:sameAs links are deliberately not part
of the input: with them, OWL-RL would copy every fact of a linked book onto its
Wikidata and DBpedia IRIs (rule eq-rep-s), duplicating the graph without new knowledge.

Usage: python reasoning/materialize.py [--data D] [--ontology O] [--output inferred.ttl]
"""

import argparse
import json
import time
from collections import Counter
from pathlib import Path

from owlrl import DeductiveClosure, OWLRL_Semantics
from rdflib import BNode, Graph, Literal
from rdflib.namespace import OWL, RDF, RDFS

ROOT = Path(__file__).resolve().parent.parent


def keep(triple) -> bool:
    """Drop OWL-RL bookkeeping that carries no information."""
    s, p, o = triple
    if isinstance(s, (Literal, BNode)) or isinstance(o, BNode): return False
    if p == OWL.sameAs and s == o: return False                       # reflexive sameAs
    if p == RDF.type and o in (OWL.Thing, OWL.Class, OWL.ObjectProperty, OWL.DatatypeProperty,
                               OWL.NamedIndividual, OWL.AnnotationProperty, RDFS.Datatype): return False
    return True


def materialize(data: Path, ontology: Path, output: Path) -> dict:
    started = time.time()
    graph = Graph()
    graph.parse(ontology, format="turtle")
    graph.parse(data, format="turtle")
    asserted = set(graph)
    DeductiveClosure(OWLRL_Semantics, axiomatic_triples=False, datatype_axioms=False).expand(graph)
    inferred = Graph()
    for prefix, ns in graph.namespaces(): inferred.bind(prefix, ns)
    for triple in graph:
        if triple not in asserted and keep(triple): inferred.add(triple)
    output.parent.mkdir(parents=True, exist_ok=True)
    inferred.serialize(output, format="turtle")
    by_predicate = Counter(p.n3(inferred.namespace_manager) for _, p, _ in inferred)
    by_type = Counter(o.n3(inferred.namespace_manager) for _, _, o in inferred.triples((None, RDF.type, None)))
    return {"asserted": len(asserted), "closure": len(graph), "inferred_kept": len(inferred),
            "seconds": round(time.time() - started), "top_predicates": dict(by_predicate.most_common(15)),
            "inferred_types": dict(by_type.most_common(25))}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, default=ROOT / "pipeline/output/books_data.ttl")
    parser.add_argument("--ontology", type=Path, default=ROOT / "ontology.ttl")
    parser.add_argument("--output", type=Path, default=ROOT / "reasoning/output/inferred.ttl")
    args = parser.parse_args()
    print(json.dumps(materialize(args.data, args.ontology, args.output), indent=1))


if __name__ == "__main__": main()
