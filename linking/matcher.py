"""Conservative Wikidata-only identity gates for the seven approved targets."""
from __future__ import annotations

from core import Candidate, Local, LocalGraph, best_similarity, decide, norm, similarity, weighted, year
from external import ExternalError, QID, Wikidata, canonical_isbn, claim_values, isbn_digits, qid

TYPES = {
    "Person": {"Q5"}, "BookSeries": {"Q277759"}, "Language": {"Q33742"},
    "Publisher": {"Q2085381", "Q1320047", "Q1114515"},
    "Place": {"Q515", "Q6256", "Q486972", "Q82794", "Q618123", "Q3895768", "Q2221906"},
}
AUTHOR_OCCUPATIONS = {"Q49757", "Q6625963", "Q482980", "Q36180", "Q4853732"}


def wd_uri(item: str) -> str:
    return f"http://www.wikidata.org/entity/{item}"


def title_similarity(local: str, external: str) -> float:
    a, b = norm(local), norm(external)
    if a and b and (a == b or a.startswith(b + " ") or b.startswith(a + " ")):
        return 100.0
    return similarity(local, external)


def accepted_qids(uris, accepted):
    return {row["candidate_id"] for uri in uris if (row := accepted.get(uri))
            and row.get("decision") == "ACCEPTED" and QID.fullmatch(row.get("candidate_id") or "")}


def wikidata_context_eligible(local: Local, accepted: dict[str, dict]) -> bool:
    if local.target not in {"Person", "BookSeries", "Publisher"}:
        return True
    if local.target == "Person" and "author" not in local.data["roles"]:
        return False
    uris = local.data["editions"] if local.target == "Publisher" else local.data["books"]
    return bool(accepted_qids(uris, accepted))


class Matcher:
    def __init__(self, graph: LocalGraph, wd: Wikidata, config: dict, accepted: dict[str, dict],
                 context: dict | None = None):
        self.graph, self.wd, self.config, self.accepted = graph, wd, config, accepted
        self.context = context if context is not None else {}
        self.context.setdefault("entities", {})
        self.context.setdefault("edition_type_flags", {})
        self.context.setdefault("inverse_links", {})
        self.context.setdefault("isbn_matches", {})
        self.context.setdefault("text_search_ids", {})
        self.context.setdefault("errors", {})

    def _entity(self, item):
        if item in self.context["errors"]:
            raise self.context["errors"][item]
        if item not in self.context["entities"]:
            records = self.wd.entities([item])
            self.context["entities"][item] = records.get(item, {"missing": "entity"})
        return self.context["entities"][item]

    def _type_flags(self, item):
        if item in self.context["errors"]:
            raise self.context["errors"][item]
        if item not in self.context["edition_type_flags"]:
            self.context["edition_type_flags"].update(self.wd.edition_type_flags([item]))
        if item not in self.context["edition_type_flags"]:
            raise ExternalError("MALFORMED_RESPONSE", f"No type response for {item}")
        return self.context["edition_type_flags"][item]

    def _finish(self, local, candidates, strategy, required=True, forced="", network=True):
        row = decide(local, candidates, self.config, strategy, required, forced)
        row["network_attempted"] = network
        row["candidate_generation_path"] = strategy
        return row

    def _no_match(self, local, reason, strategy, network=False):
        return self._finish(local, [], strategy, False, reason, network)

    def _titles(self, entity):
        return sorted({x for x in self.wd.labels(entity) + claim_values(entity, "P1476") if x})

    def _edition_candidate(self, local, item, properties):
        entity = self._entity(item)
        flags = self._type_flags(item)
        book = self.graph.entities["Book"].get(local.data.get("book", ""))
        title = book.data["title"] if book else ""
        titles = self._titles(entity)
        title_score = max((title_similarity(title, x) for x in titles), default=0)
        compatible = flags["edition"] or flags["publication"]
        features = {"isbn": 100, "type": 100 if compatible else 0, "title": title_score}
        conflicts = []
        if not compatible:
            conflicts.append("WRONG_ENTITY_TYPE")
        if not title or not titles:
            conflicts.append("INSUFFICIENT_TITLE_SANITY")
        elif title_score < 65:
            conflicts.append("TITLE_CONTRADICTION")
        external_year = year(next(iter(claim_values(entity, "P577")), ""))
        if local.data["year"] and external_year and abs(int(local.data["year"]) - int(external_year)) > 12:
            conflicts.append("YEAR_CONTRADICTION")
        external_pages = next(iter(claim_values(entity, "P1104")), "")
        if local.data["pages"].isdigit() and external_pages.isdigit():
            a, b = int(local.data["pages"]), int(external_pages)
            if abs(a - b) > 50 and abs(a - b) / max(a, b) > 0.3:
                conflicts.append("PAGES_CONTRADICTION")
        evidence = {"candidate_generation_path": "exact_P212_P957", "canonical_isbn13": canonical_isbn(local.data["isbn"]),
                    "matched_properties": sorted(properties), "titles": titles,
                    "direct_p31": claim_values(entity, "P31"), "edition_type_flags": flags,
                    "external_year": external_year, "external_pages": external_pages}
        return Candidate(wd_uri(item), item, features, weighted(features, self.config["weights"]["BookEdition"]),
                         conflicts, evidence)

    def edition(self, local):
        canonical = canonical_isbn(local.data["isbn"])
        if not canonical:
            return self._no_match(local, "INVALID_ISBN", "isbn", False)
        if canonical in self.context["errors"]:
            raise self.context["errors"][canonical]
        if canonical not in self.context["isbn_matches"]:
            original10 = {canonical: {isbn_digits(local.data["isbn"])}} if len(isbn_digits(local.data["isbn"])) == 10 else {}
            self.context["isbn_matches"].update(self.wd.isbn_candidates([canonical], original10))
        matches = self.context["isbn_matches"].get(canonical, {})
        if not matches:
            return self._no_match(local, "ISBN_NOT_FOUND", "isbn", True)
        candidates = [self._edition_candidate(local, item, properties) for item, properties in sorted(matches.items())]
        plausible = sum(not x.conflicts for x in candidates)
        forced = "MULTIPLE_ISBN_CANDIDATES" if plausible > 1 else ""
        return self._finish(local, candidates, "isbn", forced=forced)

    def _inverse(self, edition):
        if edition in self.context["errors"]:
            raise self.context["errors"][edition]
        if edition not in self.context["inverse_links"]:
            self.context["inverse_links"].update(self.wd.inverse_work_links([edition]))
        return self.context["inverse_links"].get(edition, set())

    def _book_candidate(self, local, item, paths):
        entity = self._entity(item)
        flags = self._type_flags(item)
        titles = self._titles(entity)
        title_score = max((title_similarity(local.data["title"], x) for x in titles), default=0)
        author_ids = [x for x in claim_values(entity, "P50") if QID.fullmatch(x)]
        author_names = [name for author in author_ids for name in self.wd.labels(self._entity(author)) if name]
        author_score = max((best_similarity(name, author_names) for name in local.data["authors"]), default=0) if author_names else 100
        features = {"anchor": 100, "title": title_score, "author": author_score}
        conflicts = []
        if flags["edition"] or flags["publication"] or not claim_values(entity, "P31"):
            conflicts.append("ABSTRACTION_LEVEL_MISMATCH")
        if not titles or title_score < 65:
            conflicts.append("TITLE_CONTRADICTION")
        if author_names and local.data["authors"] and author_score < 35:
            conflicts.append("AUTHOR_CONTRADICTION")
        evidence = {"candidate_generation_path": "edition_P629_or_inverse_P747", "edition_paths": paths,
                    "titles": titles, "direct_p31": claim_values(entity, "P31"),
                    "edition_type_flags": flags, "p50_authors": author_ids, "p50_author_names": author_names}
        return Candidate(wd_uri(item), item, features, weighted(features, self.config["weights"]["Book"]),
                         conflicts, evidence)

    def book(self, local):
        editions = accepted_qids(local.data["editions"], self.accepted)
        if not editions:
            return self._no_match(local, "INSUFFICIENT_CONTEXT", "edition_anchor", False)
        paths = {}
        for edition in sorted(editions):
            record = self._entity(edition)
            p629 = {x for x in claim_values(record, "P629") if QID.fullmatch(x)}
            p747 = self._inverse(edition)
            for item in p629 | p747:
                paths.setdefault(item, []).append({"edition_qid": edition,
                                                    "properties": sorted((["P629"] if item in p629 else []) +
                                                                         (["inverse_P747"] if item in p747 else []))})
        if not paths:
            return self._no_match(local, "EDITION_FOUND_NO_WORK", "edition_anchor", True)
        if len(paths) > 1:
            return self._no_match(local, "INCOMPATIBLE_EDITION_WORKS", "edition_anchor", True)
        item = next(iter(paths))
        return self._finish(local, [self._book_candidate(local, item, paths[item])], "edition_anchor")

    def _direct_ids(self, local):
        if local.target == "Person":
            if "author" not in local.data["roles"]:
                return set(), set()
            anchors = accepted_qids(local.data["books"], self.accepted)
            property_id = "P50"
        elif local.target == "BookSeries":
            anchors = accepted_qids(local.data["books"], self.accepted)
            property_id = "P179"
        else:
            anchors = accepted_qids(local.data["editions"], self.accepted)
            property_id = "P123"
        candidates = {x for anchor in anchors for x in claim_values(self._entity(anchor), property_id) if QID.fullmatch(x)}
        return anchors, candidates

    def _direct_candidate(self, local, item, anchors, count):
        entity = self._entity(item)
        property_id = {"Person": "P50", "BookSeries": "P179", "Publisher": "P123"}[local.target]
        supporting, contradictory = [], []
        for anchor in sorted(anchors):
            stated = {x for x in claim_values(self._entity(anchor), property_id) if QID.fullmatch(x)}
            if item in stated:
                supporting.append(anchor)
            elif stated:
                contradictory.append(anchor)
        types = self.wd.types(entity)
        valid_type = bool(types & TYPES[local.target])
        labels = self.wd.labels(entity)
        name = best_similarity(local.label, labels)
        features = {"name": name, "type": 100 if valid_type else 0}
        conflicts = [] if valid_type else ["WRONG_ENTITY_TYPE"]
        if contradictory:
            conflicts.append("CONTEXT_CONTRADICTION")
        evidence = {"candidate_generation_path": {"Person": "work_P50", "BookSeries": "work_P179",
                                                   "Publisher": "edition_P123"}[local.target],
                    "anchor_qids": sorted(anchors), "supporting_anchor_qids": supporting,
                    "contradictory_anchor_qids": contradictory, "labels": labels, "types": sorted(types),
                    "claims": {p: claim_values(entity, p) for p in ("P31", "P106", "P749", "P127", "P1366")}}
        if local.target == "Person":
            occupations = set(claim_values(entity, "P106"))
            role_match = not occupations or bool(occupations & AUTHOR_OCCUPATIONS)
            features.update(role=100 if role_match else 0, context=100)
            if occupations and not role_match:
                conflicts.append("ROLE_CONTRADICTION")
            if name < 90:
                conflicts.append("INSUFFICIENT_LABEL_MATCH")
            evidence["local_roles"] = local.data["roles"]
        elif local.target == "BookSeries":
            features.update(members=100, context=100)
            if name < 85:
                conflicts.append("INSUFFICIENT_LABEL_MATCH")
        else:
            features["context"] = 100
            if name < 90:
                conflicts.append("INSUFFICIENT_LABEL_MATCH")
            related_ids = {x for p in ("P749", "P127", "P1366") for x in claim_values(entity, p) if QID.fullmatch(x)}
            related_names = [n for related in related_ids for n in self.wd.labels(self._entity(related)) if n]
            if any(similarity(local.label, n) >= 90 for n in related_names):
                conflicts.append("PUBLISHER_RELATED_ENTITY_CONFLICT")
            evidence["related_qids"] = sorted(related_ids)
        return Candidate(wd_uri(item), item, features,
                         weighted(features, self.config["weights"][local.target]), conflicts, evidence)

    def wikidata(self, local):
        if local.target in {"Person", "BookSeries", "Publisher"}:
            strategy = {"Person": "direct_p50", "BookSeries": "direct_p179",
                        "Publisher": "direct_p123"}[local.target]
            if not wikidata_context_eligible(local, self.accepted):
                return self._no_match(local, "INSUFFICIENT_CONTEXT", strategy, False)
            anchors, ids = self._direct_ids(local)
            if not ids:
                return self._no_match(local, "INSUFFICIENT_CONTEXT", strategy, False)
            if len(ids) > self.config["candidate_limits"][local.target]:
                return self._no_match(local, "AMBIGUOUS_CANDIDATES", strategy, False)
            candidates = [self._direct_candidate(local, item, anchors, len(ids)) for item in sorted(ids)]
            if local.target == "Publisher":
                plausible = {x.candidate_id for x in candidates if not x.conflicts}
                for item in plausible:
                    entity = self._entity(item)
                    related = {x for p in ("P749", "P127", "P1366") for x in claim_values(entity, p)}
                    if related & plausible:
                        return self._finish(local, candidates, strategy, forced="PUBLISHER_RELATED_ENTITY_CONFLICT")
            return self._finish(local, candidates, strategy)
        limit = self.config["candidate_limits"][local.target]
        if local.uri in self.context["errors"]:
            raise self.context["errors"][local.uri]
        ids = self.context["text_search_ids"].get(local.uri)
        if ids is None:
            ids = self.wd.search(local.label, limit)
        entities = {item: self._entity(item) for item in ids}
        candidates = []
        for item in ids:
            entity = entities.get(item, {})
            if entity.get("missing") is not None:
                continue
            types = self.wd.types(entity)
            valid_type = bool(types & TYPES[local.target])
            labels = self.wd.labels(entity)
            name = best_similarity(local.label, labels)
            features = {"name": name, "type": 100 if valid_type else 0}
            conflicts = [] if valid_type else ["WRONG_ENTITY_TYPE"]
            if local.target == "Language":
                if name < 95:
                    conflicts.append("INSUFFICIENT_LABEL_MATCH")
            else:
                features["unique"] = 100 if len(ids) == 1 and name >= 98 else 0
                if len(ids) > 1:
                    conflicts.append("AMBIGUOUS_CANDIDATES")
                if name < 98:
                    conflicts.append("INSUFFICIENT_LABEL_MATCH")
            candidates.append(Candidate(wd_uri(item), item, features,
                                        weighted(features, self.config["weights"][local.target]),
                                        conflicts, {"candidate_generation_path": "wbsearchentities",
                                                    "labels": labels, "types": sorted(types)}))
        return self._finish(local, candidates, local.target)
