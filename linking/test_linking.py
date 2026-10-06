"""Offline semantic and operational tests for the Wikidata-only production linker."""
import json
from pathlib import Path
import sys
from io import BytesIO
from urllib.error import HTTPError

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import BOOKS, Candidate, Local, LocalGraph, Store, config_digest, decide, diagnostics
from external import ExternalError, HTTPClient, Wikidata, canonical_isbn, isbn_variants
from external import API
import external
from link_books import export, load_config
import link_books
from matcher import Matcher, wikidata_context_eligible

CONFIG = load_config(Path(__file__).with_name("config.json"))


def wd_entity(item, label, type_id, claims=None, aliases=None):
    values = {"P31": [type_id]}
    values.update(claims or {})
    return {"id": item, "labels": {"en": {"value": label}},
            "aliases": {"en": [{"value": x} for x in aliases or []]},
            "claims": {prop: [{"mainsnak": {"datavalue": {"value": ({"id": x} if x.startswith("Q") else x)}}}
                              for x in items] for prop, items in values.items()}}


def fixture(tmp_path, two_editions=False, translator=False):
    g = Graph()
    uri = lambda kind, n: URIRef(f"http://example.org/books/resource/{kind}/{n}")
    book, edition, person, series = (uri("book", 1), uri("edition", 1), uri("person", 1), uri("series", 1))
    publisher, language, place = (uri("publisher", 1), uri("language", 1), uri("place", 1))
    for resource, cls, label in ((book, BOOKS.Book, "The Example Book"),
                                 (edition, BOOKS.BookEdition, "The Example Book"),
                                 (person, BOOKS.Person, "Alice Writer"),
                                 (series, BOOKS.BookSeries, "Example Cycle"),
                                 (publisher, BOOKS.Publisher, "Example Press"),
                                 (language, BOOKS.Language, "English"),
                                 (place, BOOKS.Place, "Springfield")):
        g.add((resource, RDF.type, cls)); g.add((resource, RDFS.label, Literal(label)))
    g.add((book, BOOKS.title, Literal("The Example Book")))
    g.add((book, BOOKS.isWrittenBy, person)); g.add((book, BOOKS.hasEdition, edition))
    g.add((book, BOOKS.isPartOfSeries, series)); g.add((book, BOOKS.setIn, place))
    g.add((edition, BOOKS.isbn, Literal("9780439023481")))
    g.add((edition, BOOKS.isPublishedBy, publisher)); g.add((edition, BOOKS.isInLanguage, language))
    if two_editions:
        other = uri("edition", 2)
        g.add((other, RDF.type, BOOKS.BookEdition)); g.add((book, BOOKS.hasEdition, other))
        g.add((other, BOOKS.isbn, Literal("9780060256654")))
    if translator:
        other = uri("person", 2)
        g.add((other, RDF.type, BOOKS.Person)); g.add((other, RDFS.label, Literal("Terry Translator")))
        g.add((edition, BOOKS.isTranslatedBy, other))
    path = tmp_path / "source.ttl"
    g.serialize(path, format="turtle")
    return LocalGraph(path), path


class FakeWD:
    def __init__(self):
        self.data = {
            "Q20": wd_entity("Q20", "The Example Book", "Q3331189", {"P212": ["9780439023481"], "P629": ["Q10"], "P123": ["Q50"]}),
            "Q10": wd_entity("Q10", "The Example Book", "Q571", {"P50": ["Q30"], "P179": ["Q40"]}),
            "Q30": wd_entity("Q30", "Alice Writer", "Q5", {"P106": ["Q49757"]}),
            "Q40": wd_entity("Q40", "Example Cycle", "Q277759"),
            "Q50": wd_entity("Q50", "Example Press", "Q2085381"),
            "Q60": wd_entity("Q60", "English", "Q33742"),
            "Q70": wd_entity("Q70", "Springfield", "Q515"),
        }
        self.lookup = {"9780439023481": {"Q20": {"P212"}}}
        self.flags = {"Q20": {"edition": True, "publication": False},
                      "Q10": {"edition": False, "publication": False}}
        self.inverse = {}
        self.search_map = {"English": ["Q60"], "Springfield": ["Q70"]}
        self.calls = []
        self.fail_search = False

    def isbn_candidates(self, canonical, original10):
        self.calls.append(("isbn", tuple(canonical), original10))
        return {x: self.lookup.get(x, {}) for x in canonical}

    def entities(self, ids):
        self.calls.append(("entities", tuple(ids)))
        return {x: self.data[x] for x in ids if x in self.data}

    def edition_type_flags(self, ids):
        self.calls.append(("flags", tuple(ids)))
        return {x: self.flags.get(x, {"edition": False, "publication": False}) for x in ids}

    def inverse_work_links(self, ids):
        self.calls.append(("inverse", tuple(ids)))
        return {x: set(self.inverse.get(x, set())) for x in ids}

    def search(self, label, limit):
        self.calls.append(("search", label))
        if self.fail_search:
            raise ExternalError("TRANSIENT_NETWORK_ERROR", "timeout")
        return self.search_map.get(label, [])[:limit]

    def labels(self, entity):
        return [entity.get("labels", {}).get("en", {}).get("value", "")] + [
            x["value"] for x in entity.get("aliases", {}).get("en", [])]

    def types(self, entity, depth=2):
        return {x["mainsnak"]["datavalue"]["value"]["id"] for x in entity.get("claims", {}).get("P31", [])}


def m(graph, wd=None, accepted=None, context=None):
    return Matcher(graph, wd or FakeWD(), CONFIG, accepted or {}, context)


def local(graph, target):
    return next(iter(graph.entities[target].values()))


def accepted(local_entity, item):
    return {local_entity.uri: {"decision": "ACCEPTED", "candidate_id": item, "target_type": local_entity.target}}


@pytest.mark.parametrize("raw,expected", [("0439023483", "9780439023481"), ("0439023484", ""),
                                           ("097522980X", "9780975229804"),
                                           ("9780439023481", "9780439023481"),
                                           ("9780439023482", ""), ("2940016356938", ""),
                                           ("0000195166000", ""), ("9791090636071", "9791090636071")])
def test_checksum_and_prefix(raw, expected):
    assert canonical_isbn(raw) == expected


def test_exact_p212_and_p957_paths(tmp_path):
    graph, _ = fixture(tmp_path)
    edition = local(graph, "BookEdition")
    wd = FakeWD()
    row = m(graph, wd).edition(edition)
    assert row["decision"] == "ACCEPTED" and row["reason_code"] == "EXACT_ISBN"
    assert row["candidate_uri"] == "http://www.wikidata.org/entity/Q20"
    assert row["candidates"][0]["evidence"]["matched_properties"] == ["P212"]
    edition.data["isbn"] = "0439023483"
    wd.lookup["9780439023481"] = {"Q20": {"P957"}}
    row = m(graph, wd).edition(edition)
    assert row["decision"] == "ACCEPTED"
    assert row["candidates"][0]["evidence"]["matched_properties"] == ["P957"]
    assert any(call[0] == "isbn" and call[2] == {"9780439023481": {"0439023483"}} for call in wd.calls)


def test_invalid_isbn_and_not_found_no_text_fallback(tmp_path):
    graph, _ = fixture(tmp_path)
    edition = local(graph, "BookEdition")
    wd = FakeWD()
    edition.data["isbn"] = "0439023484"
    row = m(graph, wd).edition(edition)
    assert row["reason_code"] == "INVALID_ISBN" and wd.calls == []
    edition.data["isbn"] = "9780439023481"
    wd.lookup.clear()
    row = m(graph, wd).edition(edition)
    assert row["reason_code"] == "ISBN_NOT_FOUND"
    assert not any(call[0] == "search" for call in wd.calls)


def test_multiple_isbn_candidates_wrong_type_and_title_contradiction(tmp_path):
    graph, _ = fixture(tmp_path)
    edition = local(graph, "BookEdition")
    wd = FakeWD()
    wd.data["Q21"] = wd_entity("Q21", "The Example Book", "Q3331189")
    wd.flags["Q21"] = {"edition": True, "publication": False}
    wd.lookup[edition.data["isbn"]]["Q21"] = {"P212"}
    assert m(graph, wd).edition(edition)["reason_code"] == "MULTIPLE_ISBN_CANDIDATES"
    wd.lookup[edition.data["isbn"]] = {"Q21": {"P212"}}
    wd.flags["Q21"] = {"edition": False, "publication": False}
    assert m(graph, wd).edition(edition)["reason_code"] == "WRONG_ENTITY_TYPE"
    wd.flags["Q21"] = {"edition": True, "publication": False}
    wd.data["Q21"]["labels"]["en"]["value"] = "An Unrelated Atlas"
    assert m(graph, wd).edition(edition)["reason_code"] == "TITLE_CONTRADICTION"


def test_external_isbn_lookup_rechecks_returned_checksum():
    class FakeHTTP:
        def __init__(self): self.calls = []
        def post(self, url, params):
            self.calls.append(params["query"])
            return {"results": {"bindings": [{"item": {"value": "http://www.wikidata.org/entity/Q1"},
                                                 "isbn": {"value": "9780439023482"}}]}}
    wd = Wikidata(FakeHTTP())
    assert wd.isbn_candidates(["9780439023481"], {}) == {"9780439023481": {}}
    assert "P212" in wd.http.calls[0]
    assert "978-0-439-02348-1" in isbn_variants("9780439023481")


def test_structured_p957_lookup_uses_original_valid_isbn10():
    class FakeHTTP:
        def __init__(self): self.queries = []
        def post(self, url, params):
            query = params["query"]
            self.queries.append(query)
            rows = [{"item": {"value": "http://www.wikidata.org/entity/Q20"},
                     "isbn": {"value": "0-439-02348-3"}}] if "/P957>" in query else []
            return {"results": {"bindings": rows}}
    http = FakeHTTP()
    result = Wikidata(http).isbn_candidates(["9780439023481"], {"9780439023481": {"0439023483"}})
    assert result == {"9780439023481": {"Q20": {"P957"}}}
    assert any("/P957>" in query for query in http.queries)


def test_book_p629_and_inverse_p747(tmp_path):
    graph, _ = fixture(tmp_path)
    book, edition = local(graph, "Book"), local(graph, "BookEdition")
    wd = FakeWD()
    row = m(graph, wd, accepted(edition, "Q20")).book(book)
    assert row["decision"] == "ACCEPTED" and row["reason_code"] == "EDITION_ANCHORED_WORK"
    assert row["candidate_id"] == "Q10"
    wd.data["Q20"]["claims"].pop("P629")
    wd.inverse["Q20"] = {"Q10"}
    inverse = m(graph, wd, accepted(edition, "Q20")).book(book)
    assert inverse["decision"] == "ACCEPTED"
    assert inverse["candidates"][0]["evidence"]["edition_paths"][0]["properties"] == ["inverse_P747"]
    wd.inverse.clear()
    assert m(graph, wd, accepted(edition, "Q20")).book(book)["reason_code"] == "EDITION_FOUND_NO_WORK"


def test_conflicting_work_and_abstraction(tmp_path):
    graph, _ = fixture(tmp_path, two_editions=True)
    book = local(graph, "Book")
    editions = list(graph.entities["BookEdition"].values())
    wd = FakeWD()
    wd.data["Q21"] = wd_entity("Q21", "The Example Book", "Q3331189", {"P629": ["Q11"]})
    wd.data["Q11"] = wd_entity("Q11", "The Example Book", "Q571")
    wd.flags["Q11"] = {"edition": False, "publication": False}
    anchors = {editions[0].uri: {"decision": "ACCEPTED", "candidate_id": "Q20"},
               editions[1].uri: {"decision": "ACCEPTED", "candidate_id": "Q21"}}
    assert m(graph, wd, anchors).book(book)["reason_code"] == "INCOMPATIBLE_EDITION_WORKS"
    wd.flags["Q10"] = {"edition": True, "publication": False}
    assert m(graph, wd, {editions[0].uri: anchors[editions[0].uri]}).book(book)["reason_code"] == "ABSTRACTION_LEVEL_MISMATCH"


def test_person_direct_p50_no_name_search_and_non_author_policy(tmp_path):
    graph, _ = fixture(tmp_path, translator=True)
    book, person = local(graph, "Book"), next(x for x in graph.entities["Person"].values() if "author" in x.data["roles"])
    translator = next(x for x in graph.entities["Person"].values() if "translator" in x.data["roles"])
    wd = FakeWD()
    early = m(graph, wd).wikidata(person)
    assert early["reason_code"] == "INSUFFICIENT_CONTEXT" and early["network_attempted"] is False and wd.calls == []
    anchors = accepted(book, "Q10")
    assert wikidata_context_eligible(person, anchors)
    row = m(graph, wd, anchors).wikidata(person)
    assert row["decision"] == "ACCEPTED" and row["reason_code"] == "DIRECT_P50_CONTEXT"
    assert not any(call[0] == "search" for call in wd.calls)
    wd.calls.clear()
    pure = m(graph, wd, anchors).wikidata(translator)
    assert pure["reason_code"] == "INSUFFICIENT_CONTEXT" and wd.calls == []
    person.data["roles"] = ["author", "editor"]
    assert m(graph, wd, anchors).wikidata(person)["decision"] == "ACCEPTED"
    wd.data["Q10"]["claims"].pop("P50")
    assert m(graph, wd, anchors).wikidata(person)["reason_code"] == "INSUFFICIENT_CONTEXT"


@pytest.mark.parametrize("occupation,decision,reason", [
    ("Q36180", "ACCEPTED", "DIRECT_P50_CONTEXT"),
    ("Q250867", "NO_MATCH", "ROLE_CONTRADICTION"),
])
def test_author_occupation_qid_requires_actual_writer_occupation(tmp_path, occupation, decision, reason):
    graph, _ = fixture(tmp_path)
    book, person = local(graph, "Book"), local(graph, "Person")
    wd = FakeWD()
    wd.data["Q30"] = wd_entity("Q30", "Alice Writer", "Q5", {"P106": [occupation]})

    row = m(graph, wd, accepted(book, "Q10")).wikidata(person)

    assert row["decision"] == decision
    assert row["reason_code"] == reason
    assert ("ROLE_CONTRADICTION" in row["hard_conflicts"]) == (occupation == "Q250867")
    assert row["candidates"][0]["feature_scores"]["role"] == (100 if occupation == "Q36180" else 0)
    assert not any(call[0] == "search" for call in wd.calls)


def test_explicit_direct_context_conflicts_veto_candidate(tmp_path):
    graph, _ = fixture(tmp_path)
    book, person = local(graph, "Book"), local(graph, "Person")
    wd = FakeWD()
    wd.data["Q11"] = wd_entity("Q11", "Other Work", "Q571", {"P50": ["Q31"]})
    other_uri = "http://example.org/books/resource/book/other"
    person.data["books"].append(other_uri)
    anchors = accepted(book, "Q10")
    anchors[other_uri] = {"decision": "ACCEPTED", "candidate_id": "Q11", "target_type": "Book"}
    row = m(graph, wd, anchors).wikidata(person)
    assert row["reason_code"] == "CONTEXT_CONTRADICTION"
    assert row["candidates"][0]["evidence"]["contradictory_anchor_qids"] == ["Q11"]


def test_series_direct_p179_ambiguity_and_wrong_type(tmp_path):
    graph, _ = fixture(tmp_path)
    book, series = local(graph, "Book"), local(graph, "BookSeries")
    wd = FakeWD()
    assert m(graph, wd).wikidata(series)["reason_code"] == "INSUFFICIENT_CONTEXT" and wd.calls == []
    anchors = accepted(book, "Q10")
    assert m(graph, wd, anchors).wikidata(series)["reason_code"] == "DIRECT_P179_CONTEXT"
    wd.data["Q10"]["claims"].pop("P179")
    assert m(graph, wd, anchors).wikidata(series)["reason_code"] == "INSUFFICIENT_CONTEXT"
    wd.data["Q10"]["claims"]["P179"] = wd_entity("Q", "", "", {"P179": ["Q40", "Q41"]})["claims"]["P179"]
    wd.data["Q41"] = wd_entity("Q41", "Example Cycle", "Q277759")
    assert m(graph, wd, anchors).wikidata(series)["reason_code"] == "INSUFFICIENT_MARGIN"
    wd.data["Q40"]["claims"]["P31"] = wd_entity("Q", "", "Q5")["claims"]["P31"]
    assert m(graph, wd, anchors).wikidata(series)["candidate_id"] == "Q41"
    wd.data["Q41"]["claims"]["P31"] = wd_entity("Q", "", "Q5")["claims"]["P31"]
    assert m(graph, wd, anchors).wikidata(series)["reason_code"] == "WRONG_ENTITY_TYPE"


def test_structured_candidate_limit_rejects_before_candidate_fetch(tmp_path):
    graph, _ = fixture(tmp_path)
    book, series = local(graph, "Book"), local(graph, "BookSeries")
    wd = FakeWD()
    wd.data["Q10"]["claims"]["P179"] = wd_entity(
        "Q", "", "", {"P179": [f"Q{x}" for x in range(100, 111)]})["claims"]["P179"]
    result = m(graph, wd, accepted(book, "Q10")).wikidata(series)
    assert result["reason_code"] == "AMBIGUOUS_CANDIDATES"
    assert not any(call[0] == "entities" and "Q100" in call[1] for call in wd.calls)


def test_publisher_direct_p123_and_parent_conflict(tmp_path):
    graph, _ = fixture(tmp_path)
    edition, publisher = local(graph, "BookEdition"), local(graph, "Publisher")
    wd = FakeWD()
    assert m(graph, wd).wikidata(publisher)["reason_code"] == "INSUFFICIENT_CONTEXT" and wd.calls == []
    anchors = accepted(edition, "Q20")
    assert m(graph, wd, anchors).wikidata(publisher)["reason_code"] == "DIRECT_P123_CONTEXT"
    wd.data["Q20"]["claims"].pop("P123")
    assert m(graph, wd, anchors).wikidata(publisher)["reason_code"] == "INSUFFICIENT_CONTEXT"
    wd.data["Q20"]["claims"]["P123"] = wd_entity("Q", "", "", {"P123": ["Q50"]})["claims"]["P123"]
    wd.data["Q51"] = wd_entity("Q51", "Example Press", "Q2085381")
    wd.data["Q50"]["claims"]["P749"] = wd_entity("Q", "", "", {"P749": ["Q51"]})["claims"]["P749"]
    assert m(graph, wd, anchors).wikidata(publisher)["reason_code"] == "PUBLISHER_RELATED_ENTITY_CONFLICT"


def test_language_place_remain_search_based_and_conservative(tmp_path):
    graph, _ = fixture(tmp_path)
    wd = FakeWD()
    assert m(graph, wd).wikidata(local(graph, "Language"))["decision"] == "ACCEPTED"
    place = local(graph, "Place")
    assert m(graph, wd).wikidata(place)["decision"] == "ACCEPTED"
    wd.data["Q71"] = wd_entity("Q71", "Springfield", "Q515")
    wd.search_map["Springfield"] = ["Q70", "Q71"]
    assert m(graph, wd).wikidata(place)["decision"] == "NO_MATCH"
    assert any(call[0] == "search" for call in wd.calls)


def test_text_search_qids_are_enriched_in_batch_and_errors_remain_operational(tmp_path):
    graph, _ = fixture(tmp_path)
    first = local(graph, "Language")
    second = Local("http://example.org/books/resource/language/second", "Language", "French")
    wd = FakeWD()
    wd.data["Q61"] = wd_entity("Q61", "French", "Q33742")
    wd.search_map["French"] = ["Q61"]
    context = {"entities": {}, "edition_type_flags": {}, "inverse_links": {},
               "isbn_matches": {}, "text_search_ids": {}, "errors": {}}
    link_books.prefetch_text_candidates(wd, [first, second], CONFIG, context)
    assert ("entities", ("Q60", "Q61")) in wd.calls
    assert m(graph, wd, context=context).wikidata(first)["decision"] == "ACCEPTED"
    assert m(graph, wd, context=context).wikidata(second)["decision"] == "ACCEPTED"
    assert sum(call[0] == "search" for call in wd.calls) == 2
    broken = FakeWD(); broken.fail_search = True
    failure_context = {"entities": {}, "edition_type_flags": {}, "inverse_links": {},
                       "isbn_matches": {}, "text_search_ids": {}, "errors": {}}
    link_books.prefetch_text_candidates(broken, [first], CONFIG, failure_context)
    with pytest.raises(ExternalError):
        m(graph, broken, context=failure_context).wikidata(first)


def test_operational_error_is_not_no_match(tmp_path):
    graph, _ = fixture(tmp_path)
    wd = FakeWD(); wd.fail_search = True
    with pytest.raises(ExternalError) as failure:
        m(graph, wd).wikidata(local(graph, "Place"))
    assert failure.value.code == "TRANSIENT_NETWORK_ERROR"
    store = Store(tmp_path / "state.sqlite3")
    store.save_error("scope", local(graph, "Place").uri, failure.value.code, str(failure.value))
    assert len(store.errors("scope")) == 1 and not store.results("scope")


def test_cache_resume_and_wikidata_only_export(tmp_path):
    graph, path = fixture(tmp_path)
    store = Store(tmp_path / "state.sqlite3")
    row = m(graph).edition(local(graph, "BookEdition"))
    store.save_decision("scope", row["local_uri"], config_digest(CONFIG), row)
    assert Store(tmp_path / "state.sqlite3").decision("scope", row["local_uri"], config_digest(CONFIG)) == row
    original = path.read_bytes()
    export([row], path, tmp_path / "out")
    assert path.read_bytes() == original
    final = Graph().parse(tmp_path / "out/books_5star.ttl", format="turtle")
    links = list(final.triples((None, OWL.sameAs, None)))
    assert len(links) == 1 and str(links[0][2]).startswith("http://www.wikidata.org/entity/Q")


def test_mock_sample_freeze_and_small_full_export(tmp_path, monkeypatch):
    graph, path = fixture(tmp_path)
    config = json.loads(json.dumps(CONFIG))
    config["sample_counts"] = {k: 1 for k in config["sample_counts"]}
    config["sample_anchor_isbns"] = []
    config_path = tmp_path / "config.json"; config_path.write_text(json.dumps(config))
    wd = FakeWD()
    monkeypatch.setattr(link_books, "Wikidata", lambda http: wd)
    class Args:
        command = "sample"; input = path; output = tmp_path / "output"; config = config_path
        frozen = tmp_path / "output/frozen_config.json"
    assert link_books.run(Args) == 0
    report = json.loads((Args.output / "sample/diagnostics.json").read_text())
    assert report["complete"] and report["openlibrary_requests"] == 0
    assert report["trusted_anchors"]["EXACT_ISBN"] == 1
    assert report["trusted_anchors"]["EDITION_ANCHORED_WORK"] == 1
    link_books.freeze(Args)
    Args.command = "full"
    assert link_books.run(Args) == 0
    final = Graph().parse(Args.output / "full/books_5star.ttl", format="turtle")
    assert set(graph.graph) <= set(final)
    assert all(str(obj).startswith("http://www.wikidata.org/entity/Q")
               for _, _, obj in final.triples((None, OWL.sameAs, None)))
    assert len(list(final.triples((None, OWL.sameAs, None)))) == 7


def test_regression_anchor_contracts_from_isbn_experiment(tmp_path):
    graph, _ = fixture(tmp_path)
    edition = local(graph, "BookEdition")
    book = local(graph, "Book")
    wd = FakeWD()
    for isbn, (edition_qid, work_qid) in link_books.POSITIVE_ANCHORS.items():
        wd.data[edition_qid] = wd_entity(edition_qid, "The Example Book", "Q3331189", {"P629": [work_qid]})
        wd.data[work_qid] = wd_entity(work_qid, "The Example Book", "Q571", {"P50": ["Q30"]})
        wd.flags[edition_qid] = {"edition": True, "publication": False}
        wd.flags[work_qid] = {"edition": False, "publication": False}
        wd.lookup[isbn] = {edition_qid: {"P212"}}
        edition.data["isbn"] = isbn
        row = m(graph, wd).edition(edition)
        assert row["candidate_id"] == edition_qid
        assert m(graph, wd, accepted(edition, edition_qid)).book(book)["candidate_id"] == work_qid
    assert set(link_books.NEGATIVE_ANCHORS) == {"9781401215811", "9780226468013", "9780674996274"}


def test_no_openlibrary_production_reference():
    root = Path(__file__).resolve().parent
    for name in ("matcher.py", "external.py", "link_books.py", "config.json"):
        text = (root / name).read_text(encoding="utf-8").lower()
        assert "openlibrary.org" not in text and "p648" not in text


def test_production_http_rejects_unapproved_endpoint_before_network(tmp_path):
    http = HTTPClient(Store(tmp_path / "cache.sqlite3"), CONFIG, "contact@example.org")
    with pytest.raises(ExternalError) as error:
        http.get("https://example.invalid/api", {})
    assert error.value.code == "UNAPPROVED_ENDPOINT"
    assert http.network_requests == 0 and http.network_hosts == {}


def test_http_429_retry_and_successful_response_cache(tmp_path, monkeypatch):
    config = json.loads(json.dumps(CONFIG))
    config["http"]["wikidata_interval_seconds"] = 0
    attempts = []
    def fake_urlopen(request, timeout):
        attempts.append(request.full_url)
        if len(attempts) == 1:
            raise HTTPError(request.full_url, 429, "rate limited", {"Retry-After": "0"}, None)
        return BytesIO(b'{"search": []}')
    monkeypatch.setattr(external, "urlopen", fake_urlopen)
    store = Store(tmp_path / "cache.sqlite3")
    http = HTTPClient(store, config, "contact@example.org")
    assert http.get(API, {"action": "wbsearchentities"}) == {"search": []}
    assert http.network_requests == 2
    cached = HTTPClient(Store(tmp_path / "cache.sqlite3"), config)
    assert cached.get(API, {"action": "wbsearchentities"}) == {"search": []}
    assert cached.network_requests == 0 and cached.cache_hits == 1
