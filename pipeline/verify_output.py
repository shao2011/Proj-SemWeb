"""Validate a full pipeline run and print an order-independent graph fingerprint."""

import argparse
import csv
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sys

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, XSD

sys.path.insert(0, str(Path(__file__).resolve().parent))
from books_pipeline import BOOKS, CATEGORIES, DEFAULT_BASE, select_top_ids, validate_vocabulary


def verify(data_path: Path, ontology_path: Path, csv_path: Path, qa_path: Path,
           base: str = DEFAULT_BASE) -> dict:
    graph = Graph().parse(data_path, format="turtle")
    ontology = Graph().parse(ontology_path, format="turtle")
    validate_vocabulary(graph, ontology)
    summary = json.loads((qa_path / "qa_summary.json").read_text(encoding="utf-8"))
    details = {name: json.loads((qa_path / f"{name}.json").read_text(encoding="utf-8"))
               for name in CATEGORIES}
    assert summary["triple_count"] == len(graph)
    assert summary["issue_counts"] == {name: len(rows) for name, rows in details.items()}
    assert summary["selected_rows"] == summary["parsed_rows"] + summary["skipped_rows"]

    books = set(graph.subjects(RDF.type, BOOKS.Book))
    editions = set(graph.subjects(RDF.type, BOOKS.BookEdition))
    assert len(books) == summary["Book"]
    assert len(editions) == summary["BookEdition"]
    assert not books & editions
    assert all(list(graph.objects(book, BOOKS.hasEdition)) for book in books)
    # A work may lack isWrittenBy only if every edition was reported as authorless.
    no_author_ids = {record["bookId"] for record in details["works_without_author"]}
    for book in books:
        if not list(graph.objects(book, BOOKS.isWrittenBy)):
            assert all(str(graph.value(e, BOOKS.bookId)) in no_author_ids
                       for e in graph.objects(book, BOOKS.hasEdition)), book
    assert all(len(set(graph.subjects(BOOKS.hasEdition, edition))) == 1 for edition in editions)
    assert all(isinstance(s, URIRef) and str(s).startswith(base) for s in books | editions)
    assert not any(isinstance(node, BNode) for triple in graph for node in triple)
    assert all(graph.value(edition, BOOKS.bookId) for edition in editions)
    assert all(not str(node).startswith(base) or isinstance(node, URIRef)
               for triple in graph for node in (triple[0], triple[2]))

    skip_rows = {record["row"] for record in details["skipped_rows"]}
    selected = select_top_ids(csv_path, summary["top_n"])
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        valid_ids = {row["bookId"].strip() for number, row in enumerate(csv.DictReader(handle), start=2)
                     if number not in skip_rows and (selected is None or row["bookId"].strip() in selected)}
    assert len(valid_ids) == len(editions)

    domain = {BOOKS.isbn: BOOKS.BookEdition, BOOKS.pageCount: BOOKS.BookEdition,
              BOOKS.isPublishedBy: BOOKS.BookEdition, BOOKS.hasFormat: BOOKS.BookEdition,
              BOOKS.publishYear: BOOKS.BookEdition, BOOKS.firstPublishYear: BOOKS.Book,
              BOOKS.rating: BOOKS.Book, BOOKS.numRatings: BOOKS.Book,
              BOOKS.hasCategory: BOOKS.Book, BOOKS.hasCharacter: BOOKS.Book,
              BOOKS.setIn: BOOKS.Book}
    for prop, typ in domain.items():
        assert all((subject, RDF.type, typ) in graph for subject in graph.subjects(prop, None)), prop
    datatypes = {p: o for p, o in ontology.subject_objects(RDFS.range)
                 if (p, RDF.type, OWL.DatatypeProperty) in ontology}
    for subject, prop, obj in graph:
        if prop in datatypes:
            assert isinstance(obj, Literal) and (
                obj.datatype == datatypes[prop] or
                (datatypes[prop] == XSD.string and obj.datatype is None)
            ), (subject, prop, obj)
            assert str(obj).strip().casefold() not in {"nan", "none", "null", ""}
            if obj.datatype == XSD.gYear:
                assert re.fullmatch(r"\d{4}", str(obj)), (subject, prop, obj)
            else:
                assert obj.value is not None, (subject, prop, obj)
        if isinstance(obj, Literal):
            assert obj.datatype != XSD.double
            if prop not in (BOOKS.description,):
                assert not str(obj).startswith("['"), (subject, prop, obj)
    # A commutative fingerprint allows comparing full runs without holding two
    # 2.2-million-triple graphs in memory at the same time.
    acc_xor, acc_sum = 0, 0
    mask = (1 << 256) - 1
    for triple in graph:
        encoded = b" ".join(node.n3().encode("utf-8") for node in triple)
        value = int.from_bytes(hashlib.sha256(encoded).digest(), "big")
        acc_xor ^= value
        acc_sum = (acc_sum + value) & mask
    return {"triples": len(graph), "books": len(books), "editions": len(editions),
            "fingerprint_xor": f"{acc_xor:064x}", "fingerprint_sum": f"{acc_sum:064x}"}


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=root / "pipeline/output/books_data.ttl")
    parser.add_argument("--ontology", type=Path, default=root / "ontology.ttl")
    parser.add_argument("--input", type=Path, default=root / "books_1.Best_Books_Ever.csv")
    parser.add_argument("--qa-dir", type=Path, default=root / "pipeline/output/qa")
    parser.add_argument("--resource-base", default=DEFAULT_BASE)
    args = parser.parse_args()
    print(json.dumps(verify(args.data, args.ontology, args.input, args.qa_dir,
                            args.resource_base.rstrip("/") + "/"), indent=2))


if __name__ == "__main__": main()
