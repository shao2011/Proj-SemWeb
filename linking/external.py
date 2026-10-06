"""Wikidata-only structured access, validated ISBNs, and persistent HTTP caching."""
from __future__ import annotations

import itertools
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.parse import urlparse

from core import Store

QID = re.compile(r"^Q[1-9]\d*$")
WD = "http://www.wikidata.org/"
SPARQL = "https://query.wikidata.org/sparql"
API = "https://www.wikidata.org/w/api.php"


def canonical_isbn(value):
    raw = str(value or "").strip().upper()
    if not re.fullmatch(r"[0-9X\s-]+", raw):
        return ""
    digits = re.sub(r"[\s-]", "", raw)
    if len(digits) == 10:
        if not re.fullmatch(r"[0-9]{9}[0-9X]", digits):
            return ""
        checksum = sum((10 - i) * (10 if digit == "X" else int(digit))
                       for i, digit in enumerate(digits))
        if checksum % 11:
            return ""
        stem = "978" + digits[:9]
        check = (10 - sum((1 if i % 2 == 0 else 3) * int(n) for i, n in enumerate(stem)) % 10) % 10
        return stem + str(check)
    if len(digits) == 13 and digits.isdigit() and digits.startswith(("978", "979")):
        checksum = sum((1 if i % 2 == 0 else 3) * int(n) for i, n in enumerate(digits))
        if checksum % 10 == 0:
            return digits
    return ""


def isbn_digits(value):
    """Strip presentation separators before checksum validation."""
    return re.sub(r"[\s\-\u2010-\u2015]", "", str(value or "")).upper()


def isbn_variants(digits):
    """Indexed exact-value probes for compact and conventional group splits."""
    stem, check = digits[:-1], digits[-1]
    prefix, middle = (stem[:3], stem[3:]) if len(digits) == 13 else ("", stem)
    values = {digits}
    for i, j in itertools.combinations(range(1, len(middle)), 2):
        groups = ([prefix] if prefix else []) + [middle[:i], middle[i:j], middle[j:], check]
        for separator in ("-", " "):
            values.add(separator.join(groups))
    return sorted(values)


def qid(value):
    part = str(value or "").rsplit("/", 1)[-1]
    return part if QID.fullmatch(part) else ""


def claim_values(entity: dict, prop: str) -> list[str]:
    out = []
    for statement in entity.get("claims", {}).get(prop, []):
        if statement.get("rank") == "deprecated":
            continue
        value = statement.get("mainsnak", {}).get("datavalue", {}).get("value")
        if isinstance(value, dict):
            value = value.get("id") or value.get("text")
        if value is not None:
            out.append(str(value))
    return out


class ExternalError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code


class HTTPClient:
    def __init__(self, store: Store, config: dict, contact: str = ""):
        self.store, self.config, self.contact = store, config["http"], contact
        self.last_call = 0.0
        self.network_requests = 0
        self.cache_hits = 0
        self.network_hosts = {}

    def request(self, url: str, params: dict, post: bool = False):
        if url not in {API, SPARQL}:
            raise ExternalError("UNAPPROVED_ENDPOINT", "Production Wikidata client accepts only Wikidata API and WDQS")
        encoded = urlencode(sorted(params.items()))
        key = "Wikidata:" + ("POST:" if post else "GET:") + url + "?" + encoded
        cached = self.store.response(key)
        if cached is not None:
            self.cache_hits += 1
            return cached["value"]
        if not self.contact or "@" not in self.contact:
            raise ExternalError("CONTACT_REQUIRED", "Set LINKING_CONTACT to a real contact email")
        headers = {"User-Agent": f"BooksLODLinker/2.0 ({self.contact})", "Accept": "application/json"}
        if post:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        address = url if post else url + "?" + encoded
        detail = ""
        for attempt in range(self.config["max_retries"]):
            remaining = self.config["wikidata_interval_seconds"] - (time.monotonic() - self.last_call)
            if remaining > 0:
                time.sleep(remaining)
            self.last_call = time.monotonic()
            self.network_requests += 1
            host = urlparse(url).hostname
            self.network_hosts[host] = self.network_hosts.get(host, 0) + 1
            try:
                request = Request(address, data=encoded.encode() if post else None, headers=headers)
                with urlopen(request, timeout=self.config["timeout_seconds"]) as response:
                    data = json.load(response)
                if not isinstance(data, dict) or "error" in data:
                    raise ExternalError("MALFORMED_RESPONSE", "Wikidata returned an invalid JSON object")
                self.store.save_response(key, {"value": data})
                return data
            except HTTPError as exc:
                detail = f"HTTP {exc.code} from Wikidata"
                if exc.code not in (429, 500, 502, 503, 504):
                    raise ExternalError("HTTP_ERROR", detail) from exc
                retry_after = exc.headers.get("Retry-After", "")
                delay = min(60, int(retry_after)) if retry_after.isdigit() else 2 ** attempt
            except (URLError, TimeoutError, OSError) as exc:
                detail, delay = f"{type(exc).__name__}: {exc}", 2 ** attempt
            except (ValueError, json.JSONDecodeError) as exc:
                raise ExternalError("MALFORMED_RESPONSE", str(exc)) from exc
            if attempt + 1 < self.config["max_retries"]:
                time.sleep(delay)
        raise ExternalError("TRANSIENT_NETWORK_ERROR", detail)

    def get(self, url: str, params: dict):
        return self.request(url, params)

    def post(self, url: str, params: dict):
        return self.request(url, params, True)


class Wikidata:
    def __init__(self, http: HTTPClient):
        self.http = http
        self._entity_cache = {}

    def sparql(self, query: str) -> list[dict]:
        data = self.http.post(SPARQL, {"query": query, "format": "json"})
        bindings = data.get("results", {}).get("bindings")
        if not isinstance(bindings, list):
            raise ExternalError("MALFORMED_RESPONSE", "Wikidata SPARQL missing bindings")
        return bindings

    def search(self, label: str, limit: int) -> list[str]:
        data = self.http.get(API, {"action": "wbsearchentities", "search": label,
                                   "language": "en", "type": "item", "limit": limit, "format": "json"})
        if not isinstance(data.get("search"), list):
            raise ExternalError("MALFORMED_RESPONSE", "Wikidata search missing result list")
        return [item["id"] for item in data["search"] if QID.fullmatch(item.get("id", ""))]

    def entities(self, ids: list[str]) -> dict:
        ids = sorted(set(x for x in ids if QID.fullmatch(x)))
        missing = [x for x in ids if x not in self._entity_cache]
        for start in range(0, len(missing), 50):
            group = missing[start:start + 50]
            data = self.http.get(API, {"action": "wbgetentities", "ids": "|".join(group),
                                       "props": "labels|aliases|claims", "languages": "en", "format": "json"})
            if not isinstance(data.get("entities"), dict):
                raise ExternalError("MALFORMED_RESPONSE", "Wikidata entities missing result map")
            self._entity_cache.update(data["entities"])
            for item in group:
                self._entity_cache.setdefault(item, {"missing": "entity"})
        return {item: self._entity_cache[item] for item in ids}

    def labels(self, entity):
        return [entity.get("labels", {}).get("en", {}).get("value", "")] + [
            x.get("value", "") for x in entity.get("aliases", {}).get("en", [])]

    def types(self, entity, depth=2):
        found = set(claim_values(entity, "P31"))
        frontier = found.copy()
        for _ in range(depth):
            records = self.entities(sorted(frontier))
            next_frontier = {x for record in records.values() for x in claim_values(record, "P279")} - found
            found |= next_frontier
            frontier = next_frontier
            if not frontier:
                break
        return found

    def isbn_candidates(self, canonical: list[str], original10: dict[str, set[str]]) -> dict[str, dict[str, set[str]]]:
        """Exact P212/P957 lookup for one configured batch."""
        canonical = sorted(set(x for x in canonical if canonical_isbn(x) == x))
        answer = {isbn: {} for isbn in canonical}
        if not canonical:
            return answer
        for property_id, values in (("P212", canonical),
                                    ("P957", sorted({x for isbn in canonical for x in original10.get(isbn, set())
                                                     if len(isbn_digits(x)) == 10 and canonical_isbn(x) == isbn}))):
            variants = sorted({variant for value in values for variant in isbn_variants(isbn_digits(value))})
            if not variants:
                continue
            literals = " ".join(json.dumps(x) for x in variants)
            query = ("SELECT DISTINCT ?item ?isbn WHERE { VALUES ?isbn { " + literals + " } "
                     f"?item <{WD}prop/direct/{property_id}> ?isbn . }}")
            for binding in self.sparql(query):
                item = qid(binding.get("item", {}).get("value", ""))
                normalized = canonical_isbn(isbn_digits(binding.get("isbn", {}).get("value", "")))
                if item and normalized in answer:
                    answer[normalized].setdefault(item, set()).add(property_id)
        return answer

    def edition_type_flags(self, ids: list[str]) -> dict[str, dict]:
        ids = sorted(set(x for x in ids if QID.fullmatch(x)))
        result = {}
        for start in range(0, len(ids), 25):
            group = ids[start:start + 25]
            values = " ".join(f"<{WD}entity/{item}>" for item in group)
            query = ("SELECT ?item ?edition ?publication WHERE { VALUES ?item { " + values + " } "
                     f"BIND(EXISTS {{ ?item <{WD}prop/direct/P31>/<{WD}prop/direct/P279>* <{WD}entity/Q3331189> }} AS ?edition) "
                     f"BIND(EXISTS {{ ?item <{WD}prop/direct/P31>/<{WD}prop/direct/P279>* <{WD}entity/Q732577> }} AS ?publication) "
                     "}")
            for row in self.sparql(query):
                item = qid(row.get("item", {}).get("value", ""))
                if item in group:
                    result[item] = {"edition": row.get("edition", {}).get("value") == "true",
                                    "publication": row.get("publication", {}).get("value") == "true"}
        return result

    def inverse_work_links(self, edition_ids: list[str]) -> dict[str, set[str]]:
        ids = sorted(set(x for x in edition_ids if QID.fullmatch(x)))
        result = {x: set() for x in ids}
        for start in range(0, len(ids), 25):
            group = ids[start:start + 25]
            values = " ".join(f"<{WD}entity/{item}>" for item in group)
            query = ("SELECT DISTINCT ?edition ?work WHERE { VALUES ?edition { " + values + " } "
                     f"?work <{WD}prop/direct/P747> ?edition . }}")
            for row in self.sparql(query):
                edition = qid(row.get("edition", {}).get("value", ""))
                work = qid(row.get("work", {}).get("value", ""))
                if edition in result and work:
                    result[edition].add(work)
        return result
