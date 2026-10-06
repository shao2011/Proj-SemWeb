"""Automatic Wikidata-only Books 5-star linker. Run with --help for commands."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

from core import LocalGraph, Store, TARGETS, config_digest, diagnostics
from external import ExternalError, HTTPClient, QID, Wikidata, canonical_isbn, claim_values, isbn_digits
from matcher import Matcher, accepted_qids, wikidata_context_eligible

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = ROOT / "pipeline/output/books_data.ttl"
DEFAULT_OUTPUT = ROOT / "linking/output"
CACHE_NAME = "wikidata_state.sqlite3"
POSITIVE_ANCHORS = {
    "9780060256531": ("Q124613230", "Q7380067"),
    "9780060256654": ("Q125931121", "Q2915384"),
    "9780060586584": ("Q126698055", "Q523766"),
    "9780060852559": ("Q126552441", "Q4764731"),
    "9780060880125": ("Q133853237", "Q3289777"),
}
NEGATIVE_ANCHORS = {"9781401215811": "EDITION_FOUND_NO_WORK",
                    "9780226468013": "MULTIPLE_ISBN_CANDIDATES",
                    "9780674996274": "WRONG_ENTITY_TYPE"}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def implementation_sha256():
    digest = hashlib.sha256()
    for name in ("core.py", "external.py", "matcher.py", "link_books.py"):
        digest.update((Path(__file__).parent / name).read_bytes())
    return digest.hexdigest()


def load_config(path):
    config = json.loads(path.read_text(encoding="utf-8"))
    assert config["version"] == 2, "Wikidata-only production config must be version 2"
    assert set(config["weights"]) == set(TARGETS)
    for weights in config["weights"].values():
        assert sum(weights.values()) == 100, weights
    for target in TARGETS:
        assert config["candidate_limits"][target] > 0
        assert config["thresholds"][target] >= 0
        assert config["margins"][target] >= 0
    assert 1 <= config["isbn_batch_size"] <= 50
    return config


def chunks(values, size):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def prefetch_entities(wd, ids, context):
    missing = sorted(set(ids) - set(context["entities"]) - set(context["errors"]))
    for group in chunks(missing, 50):
        try:
            records = wd.entities(group)
            for item in group:
                context["entities"][item] = records.get(item, {"missing": "entity"})
        except ExternalError as exc:
            for item in group:
                context["errors"][item] = exc


def prefetch_type_flags(wd, ids, context):
    missing = sorted(set(ids) - set(context["edition_type_flags"]) - set(context["errors"]))
    for group in chunks(missing, 25):
        try:
            flags = wd.edition_type_flags(group)
            for item in group:
                if item in flags:
                    context["edition_type_flags"][item] = flags[item]
                else:
                    context["errors"][item] = ExternalError("MALFORMED_RESPONSE", f"Missing type flags for {item}")
        except ExternalError as exc:
            for item in group:
                context["errors"][item] = exc


def prefetch_isbns(wd, editions, config, context):
    original10 = {}
    for edition in editions:
        raw = edition.data["isbn"]
        canonical = canonical_isbn(raw)
        if canonical and len(isbn_digits(raw)) == 10:
            original10.setdefault(canonical, set()).add(isbn_digits(raw))
    isbns = sorted({canonical_isbn(x.data["isbn"]) for x in editions if canonical_isbn(x.data["isbn"])})
    for group in chunks(isbns, config["isbn_batch_size"]):
        try:
            context["isbn_matches"].update(wd.isbn_candidates(group, original10))
        except ExternalError as exc:
            for isbn in group:
                context["errors"][isbn] = exc
    ids = {item for isbn in isbns if isbn not in context["errors"]
           for item in context["isbn_matches"].get(isbn, {})}
    prefetch_entities(wd, ids, context)
    prefetch_type_flags(wd, ids, context)


def prefetch_work_context(wd, accepted, context):
    editions = sorted({row["candidate_id"] for row in accepted.values()
                       if row["target_type"] == "BookEdition" and QID.fullmatch(row["candidate_id"])})
    prefetch_entities(wd, editions, context)
    for group in chunks(editions, 25):
        try:
            context["inverse_links"].update(wd.inverse_work_links(group))
        except ExternalError as exc:
            for item in group:
                context["errors"][item] = exc
    works = {x for edition in editions if edition not in context["errors"]
             for x in claim_values(context["entities"].get(edition, {}), "P629") if QID.fullmatch(x)}
    works |= {x for edition in editions for x in context["inverse_links"].get(edition, set())}
    prefetch_entities(wd, works, context)
    prefetch_type_flags(wd, works, context)
    authors = {x for work in works if work not in context["errors"]
               for x in claim_values(context["entities"].get(work, {}), "P50") if QID.fullmatch(x)}
    prefetch_entities(wd, authors, context)


def prefetch_direct_context(wd, target, accepted, context):
    anchor_target, property_id = {"Person": ("Book", "P50"), "BookSeries": ("Book", "P179"),
                                  "Publisher": ("BookEdition", "P123")}[target]
    anchors = {row["candidate_id"] for row in accepted.values() if row["target_type"] == anchor_target
               and QID.fullmatch(row["candidate_id"])}
    prefetch_entities(wd, anchors, context)
    candidates = {x for anchor in anchors if anchor not in context["errors"]
                  for x in claim_values(context["entities"].get(anchor, {}), property_id) if QID.fullmatch(x)}
    prefetch_entities(wd, candidates, context)
    if target == "Publisher":
        related = {x for item in candidates if item not in context["errors"]
                   for p in ("P749", "P127", "P1366")
                   for x in claim_values(context["entities"].get(item, {}), p) if QID.fullmatch(x)}
        prefetch_entities(wd, related, context)


def prefetch_text_candidates(wd, locals_for_target, config, context):
    for local in locals_for_target:
        if local.uri in context["text_search_ids"] or local.uri in context["errors"]:
            continue
        try:
            context["text_search_ids"][local.uri] = wd.search(
                local.label, config["candidate_limits"][local.target])
        except ExternalError as exc:
            context["errors"][local.uri] = exc
    ids = {item for local in locals_for_target
           for item in context["text_search_ids"].get(local.uri, [])}
    prefetch_entities(wd, ids, context)


def anchor_checks(results):
    problems = []
    for row in results:
        if row["decision"] != "ACCEPTED":
            continue
        item = row["candidate_id"]
        if not QID.fullmatch(item or "") or row["candidate_uri"] != f"http://www.wikidata.org/entity/{item}":
            problems.append([row["local_uri"], "NON_WIKIDATA_IDENTITY"])
        if row["target_type"] == "BookEdition":
            evidence = next((x["evidence"] for x in row["candidates"] if x["candidate_id"] == item), {})
            if row["reason_code"] != "EXACT_ISBN" or not evidence.get("matched_properties"):
                problems.append([row["local_uri"], "ISBN_ANCHOR_ERROR"])
            flags = evidence.get("edition_type_flags", {})
            if not flags.get("edition") and not flags.get("publication"):
                problems.append([row["local_uri"], "EDITION_TYPE_ERROR"])
        if row["target_type"] == "Book":
            evidence = next((x["evidence"] for x in row["candidates"] if x["candidate_id"] == item), {})
            if row["reason_code"] != "EDITION_ANCHORED_WORK" or not evidence.get("edition_paths"):
                problems.append([row["local_uri"], "WORK_ANCHOR_ERROR"])
            flags = evidence.get("edition_type_flags", {})
            if flags.get("edition") or flags.get("publication"):
                problems.append([row["local_uri"], "WORK_LEVEL_ERROR"])
    return problems


def person_role_diagnostics(people, results, errors):
    decisions = {row["local_uri"]: row for row in results if row["target_type"] == "Person"}
    failed = {row["local_uri"] for row in errors}
    report = {}
    for role in ("author", "translator", "illustrator", "editor", "narrator"):
        members = [person for person in people if role in person.data["roles"]]
        accepted = [person for person in members if person.uri in decisions
                    and decisions[person.uri]["decision"] == "ACCEPTED"]
        report[role.title()] = {"sampled": len(members), "accepted": len(accepted),
                                "accepted_via_author_context": sum("author" in person.data["roles"] for person in accepted),
                                "accepted_via_non_author_context": sum("author" not in person.data["roles"] for person in accepted),
                                "no_match": sum(person.uri in decisions and decisions[person.uri]["decision"] == "NO_MATCH" for person in members),
                                "operational_errors": sum(person.uri in failed for person in members)}
    return report


def prerequisite_links(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as handle:
        return {row["local_uri"]: row for line in handle if line.strip()
                if (row := json.loads(line))["target_type"] in {"Book", "BookEdition"}
                and row["decision"] == "ACCEPTED" and QID.fullmatch(row.get("candidate_id") or "")}


def estimate(args):
    graph = LocalGraph(args.input)
    source = args.prerequisite_decisions or args.output / "sample/decisions.jsonl"
    accepted = prerequisite_links(source)
    report = {"prerequisite_decisions": str(source), "prerequisite_file_exists": source.exists(),
              "accepted_book_anchors": sum(x["target_type"] == "Book" for x in accepted.values()),
              "accepted_edition_anchors": sum(x["target_type"] == "BookEdition" for x in accepted.values())}
    for target in ("Person", "BookSeries", "Publisher"):
        entities = graph.entities[target].values()
        eligible = sum(wikidata_context_eligible(local, accepted) for local in entities)
        total = len(graph.entities[target])
        report[target] = {"total": total, "eligible_from_saved_qids": eligible, "short_circuited": total - eligible}
        if target == "Person":
            report[target]["with_author_role"] = sum("author" in x.data["roles"] for x in entities)
    for target in ("Language", "Place"):
        report[target] = {"total": len(graph.entities[target]), "expected_text_searches": len(graph.entities[target])}
    valid_isbns = {canonical_isbn(x.data["isbn"]) for x in graph.entities["BookEdition"].values()
                   if canonical_isbn(x.data["isbn"])}
    batch_size = load_config(args.config)["isbn_batch_size"]
    report["valid_distinct_isbns"] = len(valid_isbns)
    report["estimated_isbn_p212_batches"] = (len(valid_isbns) + batch_size - 1) // batch_size
    report["estimated_wikidata_text_searches"] = report["Language"]["total"] + report["Place"]["total"]
    report["estimated_entities_skipped_before_direct_candidate_evaluation"] = sum(
        report[t]["short_circuited"] for t in ("Person", "BookSeries", "Publisher"))
    report["estimate_note"] = "Read-only snapshot using saved QID prerequisites. Full Edition/Book stages may add anchors; cache and batching change physical request totals."
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


def smoke(args):
    path = args.output / "sample/decisions.jsonl"
    if not path.exists():
        raise SystemExit("Run the bounded sample before smoke")
    rows = [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]
    editions = {}
    books = [row for row in rows if row["target_type"] == "Book"]
    for row in rows:
        if row["target_type"] != "BookEdition":
            continue
        for candidate in row["candidates"]:
            isbn = candidate["evidence"].get("canonical_isbn13")
            if isbn in POSITIVE_ANCHORS or isbn in NEGATIVE_ANCHORS:
                editions[isbn] = row
    cases = {}
    for isbn, (edition, work) in POSITIVE_ANCHORS.items():
        row = editions.get(isbn)
        book_rows = [x for x in books if x["decision"] == "ACCEPTED" and x["candidate_id"] == work
                     and any(path["edition_qid"] == edition for candidate in x["candidates"]
                             for path in candidate["evidence"].get("edition_paths", []))]
        cases[isbn] = {"expected_edition": edition, "expected_work": work,
                       "edition_decision": row["decision"] if row else "NOT_IN_SAMPLE",
                       "observed_edition": row["candidate_id"] if row else None,
                       "observed_work": work if book_rows else None,
                       "matches_experiment": bool(row and row["candidate_id"] == edition and book_rows)}
    for isbn, expected in NEGATIVE_ANCHORS.items():
        row = editions.get(isbn)
        cases[isbn] = {"expected_edge": expected, "edition_decision": row["decision"] if row else "NOT_IN_SAMPLE",
                       "edition_reason": row["reason_code"] if row else None,
                       "observed_edition": row["candidate_id"] if row else None,
                       "matches_experiment": bool(row and (row["reason_code"] == expected or
                         (expected == "EDITION_FOUND_NO_WORK" and row["decision"] == "ACCEPTED" and
                          any(x["reason_code"] == expected for x in books))))}
    report = {"sample_scope": json.loads((args.output / "sample/diagnostics.json").read_text())["scope"],
              "cases": cases, "matched_experiment": sum(x["matches_experiment"] for x in cases.values()),
              "total_cases": len(cases), "note": "These are live-sample regression observations, never hard-coded production links."}
    write_json(args.output / "wikidata_smoke/diagnostics.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


def run(args):
    config = load_config(args.config)
    digest, code_digest, input_hash = config_digest(config), implementation_sha256(), file_sha256(args.input)
    if args.command == "full":
        frozen = json.loads(args.frozen.read_text(encoding="utf-8"))
        if (frozen["config_hash"], frozen["implementation_hash"], frozen["input_sha256"]) != (digest, code_digest, input_hash):
            raise SystemExit("Frozen config/code/input differs; rerun sample and freeze")
    graph = LocalGraph(args.input)
    selected = graph.sample(config) if args.command == "sample" else {t: list(graph.entities[t].values()) for t in TARGETS}
    sample_ids = {t: [x.uri for x in selected[t]] for t in TARGETS} if args.command == "sample" else {}
    output = args.output / args.command
    output.mkdir(parents=True, exist_ok=True)
    scope = args.command + ":" + digest + ":" + code_digest[:16] + ":" + input_hash[:16]
    store = Store(args.output / CACHE_NAME)
    http = HTTPClient(store, config, os.environ.get("LINKING_CONTACT", ""))
    wd = Wikidata(http)
    context = {"entities": {}, "edition_type_flags": {}, "inverse_links": {},
               "isbn_matches": {}, "text_search_ids": {}, "errors": {}}
    accepted, all_rows = {}, []
    prefetch_isbns(wd, selected["BookEdition"], config, context)
    for target in TARGETS:
        if target == "Book":
            prefetch_work_context(wd, accepted, context)
        if target in {"Person", "BookSeries", "Publisher"}:
            prefetch_direct_context(wd, target, accepted, context)
        if target in {"Language", "Place"}:
            prefetch_text_candidates(wd, selected[target], config, context)
        matcher = Matcher(graph, wd, config, accepted, context)
        for local in selected[target]:
            row = store.decision(scope, local.uri, digest)
            if row is None:
                try:
                    row = matcher.edition(local) if target == "BookEdition" else (
                        matcher.book(local) if target == "Book" else matcher.wikidata(local))
                    row["config_hash"] = digest
                    row["implementation_hash"] = code_digest
                    row["external_provenance"] = {"source": "Wikidata", "cache_db": str(args.output / CACHE_NAME),
                                                  "network_requests_at_decision": http.network_requests,
                                                  "cache_hits_at_decision": http.cache_hits}
                    store.save_decision(scope, local.uri, digest, row)
                except ExternalError as exc:
                    store.save_error(scope, local.uri, exc.code, str(exc))
                    continue
            all_rows.append(row)
            if row["decision"] == "ACCEPTED":
                accepted[local.uri] = row
        print(f"{target}: {sum(x['target_type'] == target for x in all_rows)} decisions", flush=True)
    errors = store.errors(scope)
    expected = sum(len(x) for x in selected.values())
    problems = anchor_checks(all_rows)
    report = diagnostics(all_rows, errors, sample_ids)
    report["person_roles"] = person_role_diagnostics(selected["Person"], all_rows, errors)
    report.update({"scope": scope, "config_hash": digest, "implementation_hash": code_digest,
                   "input_sha256": input_hash, "input": str(args.input), "expected": expected,
                   "decided": len(all_rows), "complete": len(all_rows) == expected and not errors,
                   "anchor_check_problems": problems, "sample_seed": config["seed"] if args.command == "sample" else None,
                   "http_network_requests": http.network_requests, "http_cache_hits": http.cache_hits,
                   "http_request_hosts": http.network_hosts,
                   "openlibrary_requests": sum(n for host, n in http.network_hosts.items() if "openlibrary" in host),
                   "cache_db": str(args.output / CACHE_NAME)})
    write_json(output / "diagnostics.json", report)
    with (output / "decisions.jsonl").open("w", encoding="utf-8") as handle:
        for row in sorted(all_rows, key=lambda x: (TARGETS.index(x["target_type"]), x["local_uri"])):
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    with (output / "errors.jsonl").open("w", encoding="utf-8") as handle:
        for row in errors:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    if args.command == "full" and report["complete"] and not problems:
        export(all_rows, args.input, output)
    print(json.dumps({k: report[k] for k in ("expected", "decided", "error_count", "counts",
                                                  "trusted_anchors", "http_network_requests", "openlibrary_requests",
                                                  "anchor_check_problems")}, indent=2))
    return 0 if report["complete"] and not problems else 2


def export(rows, source: Path, output: Path):
    """Add accepted Wikidata identities to the byte-for-byte original Turtle."""
    output.mkdir(parents=True, exist_ok=True)
    links = sorted((row["local_uri"], row["candidate_uri"]) for row in rows if row["decision"] == "ACCEPTED")
    assert len({local for local, _ in links}) == len(links)
    for row in rows:
        if row["decision"] == "NO_MATCH":
            assert row["candidate_uri"] is None
    linkset = output / "external_links.ttl"
    with linkset.open("w", encoding="utf-8") as handle:
        handle.write("@prefix owl: <http://www.w3.org/2002/07/owl#> .\n\n")
        for local, external in links:
            assert external.startswith("http://www.wikidata.org/entity/Q")
            handle.write(f"<{local}> owl:sameAs <{external}> .\n")
    final = output / "books_5star.ttl"
    with final.open("wb") as out, source.open("rb") as original, linkset.open("rb") as additions:
        shutil.copyfileobj(original, out)
        out.write(b"\n\n")
        shutil.copyfileobj(additions, out)
    write_json(output / "output_manifest.json", {"original_sha256": file_sha256(source),
                                                 "links": len(links), "linkset": str(linkset), "final": str(final)})


def freeze(args):
    config = load_config(args.config)
    report = json.loads((args.output / "sample/diagnostics.json").read_text(encoding="utf-8"))
    if report["config_hash"] != config_digest(config) or report["implementation_hash"] != implementation_sha256():
        raise SystemExit("Code/config changed since sample; rerun sample")
    if report["input_sha256"] != file_sha256(args.input):
        raise SystemExit("4-star input changed since sample")
    if not report["complete"] or report["anchor_check_problems"] or report["openlibrary_requests"]:
        raise SystemExit("Sample must pass error, anchor, and zero-Open-Library checks before freezing")
    write_json(args.frozen, {"config_hash": report["config_hash"], "implementation_hash": report["implementation_hash"],
                             "input_sha256": report["input_sha256"], "config": config,
                             "sample_scope": report["scope"], "sample_decided": report["decided"],
                             "trusted_anchor_counts": report["trusted_anchors"],
                             "diagnostics": str(args.output / "sample/diagnostics.json")})
    print(args.frozen)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("sample", "freeze", "full", "estimate", "smoke"))
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--config", type=Path, default=ROOT / "linking/config.json")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--frozen", type=Path, default=DEFAULT_OUTPUT / "frozen_config.json")
    parser.add_argument("--prerequisite-decisions", type=Path)
    args = parser.parse_args()
    if args.command == "freeze":
        freeze(args)
    elif args.command == "estimate":
        estimate(args)
    elif args.command == "smoke":
        smoke(args)
    else:
        sys.exit(run(args))


if __name__ == "__main__":
    main()
