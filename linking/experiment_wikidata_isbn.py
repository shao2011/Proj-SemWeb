"""Isolated, read-only Wikidata ISBN coverage experiment.

No production linker decisions or owl:sameAs triples are made here.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from core import norm, similarity
from external import canonical_isbn

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "wikidata_isbn_experiment_sample_1000.csv"
OUTPUT = ROOT / "linking/output/wikidata_isbn_experiment"
ENDPOINT = "https://query.wikidata.org/sparql"
QID = re.compile(r"^Q[1-9]\d*$")
WD = "http://www.wikidata.org/"
AMBIGUOUS_BOOK_TYPES = {"Q571"}  # Generic book does not establish edition level.


def isbn_digits(value: str) -> str:
    """Strip presentation separators, then let the production checksum gate decide."""
    return re.sub(r"[\s\-\u2010-\u2015]", "", str(value or "")).upper()


def variants(digits: str) -> list[str]:
    """Index-friendly exact values: compact and conventional ISBN group splits."""
    stem, check = digits[:-1], digits[-1]
    prefix, middle = (stem[:3], stem[3:]) if len(digits) == 13 else ("", stem)
    result = {digits}
    for i, j in itertools.combinations(range(1, len(middle)), 2):
        groups = ([prefix] if prefix else []) + [middle[:i], middle[i:j], middle[j:], check]
        for separator in ("-", " "):
            result.add(separator.join(groups))
    return sorted(result)


def qid(uri: str) -> str:
    candidate = str(uri or "").rsplit("/", 1)[-1]
    return candidate if QID.fullmatch(candidate) else ""


def binding(row: dict, key: str) -> str:
    return row.get(key, {}).get("value", "")


def title_similarity(local: str, external: str) -> float:
    """Treat a full leading title followed by a subtitle as compatible."""
    a, b = norm(local), norm(external)
    if a and b and (a == b or a.startswith(b + " ") or b.startswith(a + " ")):
        return 100.0
    return similarity(local, external)


class WDQS:
    def __init__(self, output: Path, contact: str, max_new_requests: int):
        if not contact or "@" not in contact:
            raise SystemExit("Set LINKING_CONTACT to a real contact email")
        self.cache = output / "cache"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.contact = contact
        self.max_new_requests = max_new_requests
        self.new_requests = 0
        self.cache_hits = 0
        self.last_request = 0.0

    def query(self, text: str) -> list[dict]:
        digest = hashlib.sha256(text.encode()).hexdigest()
        cache_file = self.cache / f"{digest}.json"
        if cache_file.exists():
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
            self.cache_hits += 1
            return payload["results"]["bindings"]
        detail = ""
        for attempt in range(3):
            if self.new_requests >= self.max_new_requests:
                raise RuntimeError("REQUEST_BUDGET_EXCEEDED")
            delay = max(0.0, 1.5 - (time.monotonic() - self.last_request))
            if delay:
                time.sleep(delay)
            body = urlencode({"query": text, "format": "json"}).encode()
            request = Request(ENDPOINT, data=body,
                              headers={"User-Agent": f"BooksLOD-ISBN-Coverage/1.0 ({self.contact})",
                                       "Accept": "application/sparql-results+json",
                                       "Content-Type": "application/x-www-form-urlencoded"})
            self.new_requests += 1
            self.last_request = time.monotonic()
            try:
                with urlopen(request, timeout=55) as response:
                    payload = json.load(response)
                if not isinstance(payload.get("results", {}).get("bindings"), list):
                    raise ValueError("Malformed WDQS result bindings")
                cache_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                return payload["results"]["bindings"]
            except HTTPError as exc:
                detail = f"HTTP {exc.code}"
                if exc.code not in {429, 500, 502, 503, 504}:
                    break
                header = exc.headers.get("Retry-After", "")
                wait = min(60, int(header)) if header.isdigit() else (5 * 2 ** attempt)
            except (URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
                detail = f"{type(exc).__name__}: {exc}"
                wait = 5 * 2 ** attempt
            if attempt < 2:
                print(f"WDQS retry {attempt + 1}/2 after {detail}; waiting {wait}s", file=sys.stderr, flush=True)
                time.sleep(wait)
        raise RuntimeError(detail or "WDQS request failed")


def lookup_query(values: list[str], property_id: str) -> str:
    literals = " ".join(json.dumps(x) for x in sorted(set(values)))
    return ("SELECT DISTINCT ?item ?isbn WHERE { VALUES ?isbn { " + literals + " } "
            f"?item <{WD}prop/direct/{property_id}> ?isbn . }}")


def enrichment_query(qids: list[str]) -> str:
    values = " ".join(f"<{WD}entity/{x}>" for x in qids)
    return ("SELECT DISTINCT ?item ?type ?p629 ?p747work ?label ?title ?editionType ?publicationType WHERE { "
            "VALUES ?item { " + values + " } "
            f"OPTIONAL {{ ?item <{WD}prop/direct/P31> ?type . }} "
            f"OPTIONAL {{ ?item <{WD}prop/direct/P629> ?p629 . }} "
            f"OPTIONAL {{ ?p747work <{WD}prop/direct/P747> ?item . }} "
            "OPTIONAL { ?item <http://www.w3.org/2000/01/rdf-schema#label> ?label . FILTER(LANG(?label) = 'en') } "
            f"OPTIONAL {{ ?item <{WD}prop/direct/P1476> ?title . FILTER(LANG(?title) = 'en' || LANG(?title) = '') }} "
            f"BIND(EXISTS {{ ?item <{WD}prop/direct/P31>/<{WD}prop/direct/P279>* <{WD}entity/Q3331189> }} AS ?editionType) "
            f"BIND(EXISTS {{ ?item <{WD}prop/direct/P31>/<{WD}prop/direct/P279>* <{WD}entity/Q732577> }} AS ?publicationType) "
            "}")


def chunks(values: list, size: int):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def load_sample(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    required = {"bookId", "title", "author", "original_isbn", "canonical_isbn13"}
    if len(rows) != 1000 or not required <= set(rows[0]):
        raise SystemExit("Expected the exact 1,000-row sample and its five required columns")
    if len({row["canonical_isbn13"] for row in rows}) != len(rows):
        raise SystemExit("Sample canonical ISBNs are not unique")
    for row in rows:
        raw = isbn_digits(row["original_isbn"])
        canonical = canonical_isbn(raw)
        row["validated_isbn13"] = canonical if canonical == row["canonical_isbn13"] else ""
        row["validated_isbn10"] = raw if len(raw) == 10 and canonical else ""
    return rows


def result_row(source: dict, hits: set[str], properties: dict[str, set[str]],
               details: dict, error: str = "") -> dict:
    qids = sorted(hits)
    candidates = [details.get(x, {}) for x in qids]
    p629 = sorted(set().union(*(x.get("p629", set()) for x in candidates))) if candidates else []
    p747 = sorted(set().union(*(x.get("p747", set()) for x in candidates))) if candidates else []
    works = sorted(set(p629) | set(p747))
    types = {x: sorted(details.get(x, {}).get("types", set())) for x in qids}
    classifications = {x: details.get(x, {}).get("classification", "UNKNOWN") for x in qids}
    classification = classifications[qids[0]] if len(qids) == 1 else "UNKNOWN"
    titles = sorted(set().union(*(x.get("titles", set()) for x in candidates))) if candidates else []
    title_score = max((title_similarity(source["title"], title) for title in titles), default=None)
    suspicious = title_score is not None and title_score < 35
    resolved = not error and len(qids) == 1 and classification == "COMPATIBLE" and len(works) == 1
    if not source["validated_isbn13"]:
        reason = "INVALID_ISBN"
    elif error:
        reason = "OPERATIONAL_ERROR"
    elif not qids:
        reason = "ISBN_NOT_FOUND"
    elif len(qids) > 1:
        reason = "MULTIPLE_ISBN_CANDIDATES"
    elif classification != "COMPATIBLE":
        reason = "WRONG_OR_UNVERIFIED_EDITION_TYPE"
    elif not works:
        reason = "EDITION_FOUND_NO_WORK"
    elif len(works) > 1:
        reason = "MULTIPLE_WORKS"
    elif p629 and p747:
        reason = "RESOLVED_BOTH"
    elif p629:
        reason = "RESOLVED_P629"
    else:
        reason = "RESOLVED_P747"
    unknown = bool(error or not source["validated_isbn13"])
    return {"bookId": source["bookId"], "local_title": source["title"],
            "local_author": source["author"], "original_isbn": source["original_isbn"],
            "canonical_isbn13": source["canonical_isbn13"],
            "wikidata_item_count": len(qids), "wikidata_qids": qids,
            "isbn_match_properties_by_qid": {item: sorted(properties.get(item, set())) for item in qids},
            "exact_isbn_found": "unknown" if unknown and not qids else bool(qids),
            "unique_isbn_candidate": len(qids) == 1,
            "edition_type_compatible": True if classification == "COMPATIBLE" else (False if classification == "INCOMPATIBLE" else "unknown"),
            "edition_type_classification": classification,
            "edition_type_evidence": {
                "direct_p31_by_qid": types, "classification_by_qid": classifications,
                "edition_subclass_path_by_qid": {x: bool(details.get(x, {}).get("edition")) for x in qids},
                "publication_subclass_path_by_qid": {x: bool(details.get(x, {}).get("publication")) for x in qids}},
            "work_via_p629_qids": p629, "work_via_inverse_p747_qids": p747,
            "resolved_work_qids_union": works, "unique_work": len(works) == 1,
            "end_to_end_resolved": resolved, "wikidata_titles": titles,
            "title_similarity": title_score, "suspicious_title_conflict": suspicious,
            "reason_code": reason, "operational_error": error}


def rate(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None


def summarize(results: list[dict], service: WDQS, sample_hash: str) -> dict:
    n = len(results)
    hits = [r for r in results if r["exact_isbn_found"] is True]
    valid = [r for r in results if r["reason_code"] != "INVALID_ISBN"]
    count = lambda predicate: sum(bool(predicate(r)) for r in results)
    p629 = count(lambda r: bool(r["work_via_p629_qids"]))
    p747_only = count(lambda r: bool(r["work_via_inverse_p747_qids"]) and not r["work_via_p629_qids"])
    union = count(lambda r: bool(r["resolved_work_qids_union"]))
    resolved = count(lambda r: r["end_to_end_resolved"])
    categories = {
        "A_p629": lambda r: r["end_to_end_resolved"] and bool(r["work_via_p629_qids"]),
        "B_inverse_p747_only": lambda r: r["end_to_end_resolved"] and r["reason_code"] == "RESOLVED_P747",
        "C_isbn_no_work": lambda r: r["reason_code"] == "EDITION_FOUND_NO_WORK",
        "D_isbn_not_found": lambda r: r["reason_code"] == "ISBN_NOT_FOUND",
        "E_multiple_isbn": lambda r: r["reason_code"] == "MULTIPLE_ISBN_CANDIDATES",
        "F_incompatible_type": lambda r: r["edition_type_classification"] == "INCOMPATIBLE",
        "G_suspicious_title": lambda r: r["suspicious_title_conflict"],
    }
    examples = {name: [{k: r[k] for k in ("bookId", "local_title", "canonical_isbn13", "wikidata_qids",
                                          "edition_type_classification", "resolved_work_qids_union", "reason_code",
                                          "title_similarity")} for r in results if check(r)][:5]
                for name, check in categories.items()}
    return {"sample_size": n, "sample_sha256": sample_hash,
            "valid_isbn_count": len(valid), "invalid_isbn_count": n - len(valid),
            "successfully_evaluated_isbns": count(lambda r: r["reason_code"] not in {"INVALID_ISBN", "OPERATIONAL_ERROR"}),
            "isbn_found_count": len(hits), "isbn_found_rate": rate(len(hits), n),
            "isbn_found_rate_among_valid_isbns": rate(len(hits), len(valid)),
            "unique_isbn_candidate_count": count(lambda r: r["unique_isbn_candidate"]),
            "unique_isbn_candidate_rate": rate(count(lambda r: r["unique_isbn_candidate"]), n),
            "isbn_hits_via_p212_count": count(lambda r: any("P212" in values for values in r["isbn_match_properties_by_qid"].values())),
            "isbn_hits_via_p957_count": count(lambda r: any("P957" in values for values in r["isbn_match_properties_by_qid"].values())),
            "edition_type_compatible_count": count(lambda r: r["edition_type_classification"] == "COMPATIBLE"),
            "edition_type_unknown_count": count(lambda r: r["edition_type_classification"] == "UNKNOWN" and r["exact_isbn_found"] is True),
            "edition_type_unknown_missing_p31_count": count(lambda r: r["edition_type_classification"] == "UNKNOWN"
                                                         and r["exact_isbn_found"] is True and len(r["wikidata_qids"]) == 1
                                                         and not next(iter(r["edition_type_evidence"]["direct_p31_by_qid"].values()), [])),
            "edition_type_unknown_ambiguous_candidate_count": count(lambda r: r["edition_type_classification"] == "UNKNOWN"
                                                                  and len(r["wikidata_qids"]) > 1),
            "edition_type_incompatible_count": count(lambda r: r["edition_type_classification"] == "INCOMPATIBLE"),
            "work_via_p629_count": p629, "work_via_p629_rate_among_isbn_hits": rate(p629, len(hits)),
            "work_via_p747_only_count": p747_only,
            "work_via_p747_only_rate_among_isbn_hits": rate(p747_only, len(hits)),
            "work_resolved_union_count": union,
            "work_resolved_union_rate_among_isbn_hits": rate(union, len(hits)),
            "end_to_end_resolved_count": resolved, "end_to_end_resolved_rate_overall": rate(resolved, n),
            "end_to_end_rate_among_valid_isbns": rate(resolved, len(valid)),
            "end_to_end_rate_among_isbn_hits": rate(resolved, len(hits)),
            "multiple_isbn_candidate_count": count(lambda r: r["reason_code"] == "MULTIPLE_ISBN_CANDIDATES"),
            "multiple_isbn_candidate_rate": rate(count(lambda r: r["reason_code"] == "MULTIPLE_ISBN_CANDIDATES"), n),
            "multiple_isbn_candidate_rate_among_isbn_hits": rate(count(lambda r: r["reason_code"] == "MULTIPLE_ISBN_CANDIDATES"), len(hits)),
            "multiple_work_count": count(lambda r: len(r["resolved_work_qids_union"]) > 1),
            "multiple_work_rate_among_isbn_hits": rate(count(lambda r: len(r["resolved_work_qids_union"]) > 1), len(hits)),
            "operational_error_count": count(lambda r: r["reason_code"] == "OPERATIONAL_ERROR"),
            "suspicious_title_conflict_count": count(lambda r: r["suspicious_title_conflict"]),
            "reason_counts": {code: count(lambda r, code=code: r["reason_code"] == code)
                              for code in sorted({r["reason_code"] for r in results})},
            "new_http_requests": service.new_requests, "cache_hits": service.cache_hits,
            "examples": examples,
            "metric_note": "Overall rates use all 1000 supplied rows; unresolved operational errors are never treated as ISBN_NOT_FOUND. Type counts classify unique ISBN candidates only."}


def write_outputs(output: Path, rows: list[dict], report: dict, sample: Path):
    output.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0])
    with (output / "results.jsonl").open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    with (output / "results.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else value
                             for key, value in row.items()})
    (output / "diagnostics.json").write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    (output / "README.txt").write_text(
        "Wikidata ISBN coverage experiment; no links or production decisions.\n"
        f"Sample: {sample} (SHA256 {report['sample_sha256']}); fixed seed supplied: 20261004.\n"
        "Lookup: exact P212 ISBN-13 and valid original ISBN-10 P957, using compact and\n"
        "conventional hyphen/space groupings in indexed VALUES batches. Returned identifiers\n"
        "are normalized and checksum-validated before accepting a lookup hit.\n"
        "Enrichment: P31/subclass edition or publication type; P629 and inverse P747;\n"
        "English label/P1476 title is diagnostic only. Generic Q571 book type is UNKNOWN.\n"
        "End-to-end requires one ISBN candidate, compatible edition/publication type,\n"
        "and exactly one work from the P629/P747 union. No title-based matches.\n"
        "Two supplied rows fail the production ISBN validator and remain INVALID_ISBN.\n"
        "Operational errors remain separate from ISBN_NOT_FOUND. Raw successful WDQS\n"
        "responses are cached under cache/; reruns reuse them.\n"
        "This indexed exact-value strategy covers compact and conventional hyphen/space\n"
        "groupings. Unusual mixed separators or nonstandard formatting may be missed;\n"
        "the measured coverage is therefore a conservative lower bound for such values.\n",
        encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=Path, default=SAMPLE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--max-new-requests", type=int, default=150)
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 50:
        parser.error("batch size must be 1..50")
    sample = load_sample(args.sample)
    service = WDQS(args.output, os.environ.get("LINKING_CONTACT", ""), args.max_new_requests)
    hits = {row["bookId"]: set() for row in sample}
    hit_properties = {row["bookId"]: {} for row in sample}
    errors = {}
    valid = [row for row in sample if row["validated_isbn13"]]
    for number, batch in enumerate(chunks(valid, args.batch_size), 1):
        value_to_books = {}
        for row in batch:
            for value in variants(row["validated_isbn13"]):
                value_to_books.setdefault(value, set()).add(row["bookId"])
        try:
            for result in service.query(lookup_query(list(value_to_books), "P212")):
                item, raw = qid(binding(result, "item")), binding(result, "isbn")
                normalized = canonical_isbn(isbn_digits(raw))
                if item and normalized:
                    for row in batch:
                        if row["validated_isbn13"] == normalized:
                            hits[row["bookId"]].add(item)
                            hit_properties[row["bookId"]].setdefault(item, set()).add("P212")
            original10 = [row for row in batch if row["validated_isbn10"]]
            if original10:
                lookup10 = {v: row["bookId"] for row in original10 for v in variants(row["validated_isbn10"])}
                for result in service.query(lookup_query(list(lookup10), "P957")):
                    item, raw = qid(binding(result, "item")), binding(result, "isbn")
                    original = isbn_digits(raw)
                    if item and canonical_isbn(original):
                        for row in original10:
                            if canonical_isbn(original) == row["validated_isbn13"]:
                                hits[row["bookId"]].add(item)
                                hit_properties[row["bookId"]].setdefault(item, set()).add("P957")
        except RuntimeError as exc:
            for row in batch:
                errors[row["bookId"]] = str(exc)
        print(f"ISBN batch {number}: {len(batch)} rows, {sum(bool(hits[r['bookId']]) for r in batch)} hits, {len(errors)} errors; {service.new_requests} new requests", flush=True)

    all_qids = sorted({item for row in valid if row["bookId"] not in errors for item in hits[row["bookId"]]})
    details = {}
    detail_errors = {}
    for number, group in enumerate(chunks(all_qids, 25), 1):
        try:
            bindings = service.query(enrichment_query(group))
            for item in group:
                details[item] = {"types": set(), "p629": set(), "p747": set(),
                                 "titles": set(), "edition": False, "publication": False}
            for record in bindings:
                item = qid(binding(record, "item"))
                if item not in details:
                    continue
                info = details[item]
                if candidate := qid(binding(record, "type")): info["types"].add(candidate)
                if candidate := qid(binding(record, "p629")): info["p629"].add(candidate)
                if candidate := qid(binding(record, "p747work")): info["p747"].add(candidate)
                for key in ("title", "label"):
                    if title := binding(record, key): info["titles"].add(title)
                info["edition"] |= binding(record, "editionType") == "true"
                info["publication"] |= binding(record, "publicationType") == "true"
            for item in group:
                info = details[item]
                info["classification"] = ("COMPATIBLE" if info["edition"] or info["publication"] else
                                          "UNKNOWN" if not info["types"] or info["types"] <= AMBIGUOUS_BOOK_TYPES else
                                          "INCOMPATIBLE")
        except RuntimeError as exc:
            for item in group:
                detail_errors[item] = str(exc)
        print(f"Enrichment batch {number}: {len(group)} QIDs, {len(detail_errors)} errors; {service.new_requests} new requests", flush=True)

    results = []
    for row in sample:
        error = errors.get(row["bookId"], "") or next((detail_errors[x] for x in hits[row["bookId"]] if x in detail_errors), "")
        results.append(result_row(row, hits[row["bookId"]], hit_properties[row["bookId"]], details, error))
    sample_hash = hashlib.sha256(args.sample.read_bytes()).hexdigest()
    report = summarize(results, service, sample_hash)
    report["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    write_outputs(args.output, results, report, args.sample)
    print(json.dumps({key: report[key] for key in ("sample_size", "successfully_evaluated_isbns", "isbn_found_count",
                                               "end_to_end_resolved_count", "operational_error_count", "new_http_requests")}, indent=2))


if __name__ == "__main__":
    main()
