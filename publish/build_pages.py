#!/usr/bin/env python3
"""Build the static Linked Data site that makes our URIs dereferenceable on GitHub Pages.

Every subject in our namespace, for example
https://shao2011.github.io/Proj-SemWeb/resource/book/the-hunger-games--4966d945b5bd060f, gets the
file site/resource/book/the-hunger-games--4966d945b5bd060f.html, which Pages serves for the URI
without ".html". The ontology IRI and its terms (.../ontology#Book) resolve to site/ontology.html.
Pages cannot do content negotiation, so every page carries its triples twice: as an HTML table for
people and as JSON-LD (<script type="application/ld+json">) for machines.

    pipeline/.venv/bin/python publish/build_pages.py               # build site/
    pipeline/.venv/bin/python publish/build_pages.py --serve 8000  # preview site/ the way Pages serves it
"""

from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import sys
import time
from collections import defaultdict
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.collection import Collection
from rdflib.namespace import DCTERMS, OWL, RDF, RDFS, XSD, Namespace

ROOT = Path(__file__).resolve().parent.parent
BASE = "https://shao2011.github.io/Proj-SemWeb/"
ONTOLOGY = BASE + "ontology"
BOOKS = Namespace(ONTOLOGY + "#")
RESOURCE = BASE + "resource/"
DATASET = URIRef(BASE + "dataset")
VOID = Namespace("http://rdfs.org/ns/void#")
REPO = "https://github.com/shao2011/Proj-SemWeb"

# One input per named graph of the endpoint (endpoint/load.sh); the ontology is rendered separately.
INPUTS = {
    "data": ROOT / "pipeline/output/books_data.ttl",
    "inferred": ROOT / "reasoning/output/inferred.ttl",
    "links": ROOT / "linking/output/links.ttl",
    "enrichment": ROOT / "linking/output/enrichment.ttl",
    "metadata": ROOT / "endpoint/void.ttl",
}
ONTOLOGY_FILE = ROOT / "ontology.ttl"

# Display names and JSON-LD prefixes. Every namespace ends in "/" or "#", so JSON-LD 1.1 accepts
# them as prefixes of compact IRIs.
PREFIXES = {
    "books": str(BOOKS), "res": RESOURCE, "rdf": str(RDF), "rdfs": str(RDFS), "owl": str(OWL),
    "xsd": str(XSD), "schema": "https://schema.org/", "dcterms": str(DCTERMS),
    "foaf": "http://xmlns.com/foaf/0.1/", "void": str(VOID), "prov": "http://www.w3.org/ns/prov#",
    "wd": "http://www.wikidata.org/entity/", "dbr": "http://dbpedia.org/resource/",
    "olb": "https://openlibrary.org/books/",
}

CSS = """body{font:15px/1.45 system-ui,sans-serif;max-width:62rem;margin:1rem auto;padding:0 1rem;color:#222}
a{color:#0b57d0;text-decoration:none}a:hover{text-decoration:underline}
nav,footer,.note{font-size:.9em;color:#666}footer{margin-top:2em}
h1{margin:.4em 0 .1em}h3{margin:1.4em 0 0}
.uri{font:13px monospace;color:#555;word-break:break-all;margin:0 0 1em}
table{border-collapse:collapse;width:100%}
th,td{border-top:1px solid #ddd;padding:.3em .5em;vertical-align:top;text-align:left}
th{width:15em;font-weight:normal;color:#444}td{white-space:pre-line;overflow-wrap:anywhere}
em,small{color:#777}
"""

esc = partial(html.escape, quote=True)
Triples = dict[URIRef, list[tuple[URIRef, object, bool]]]  # subject -> (predicate, object, inferred)


# ---------------------------------------------------------------- URI -> file

def page_path(uri: str) -> str | None:
    """The site-relative file GitHub Pages serves for `uri`, or None when the URI is not ours."""
    s = str(uri)
    if s == ONTOLOGY or s.startswith(ONTOLOGY + "#"):
        return "ontology.html"
    if not s.startswith(BASE) or s.startswith(ONTOLOGY) or "#" in s or "?" in s:
        return None  # BASE + "ontology.ttl" is a file next to ontology.html, not a term
    rel = s[len(BASE):]
    if any(part in ("", ".", "..") for part in rel.split("/")):
        return None  # directories and anything that could escape the site
    return rel + ".html"


def root_prefix(path: str) -> str:
    """Relative link from the page at `path` to the site root."""
    return "../" * path.count("/") or "./"


def href(uri: str, up: str) -> str:
    """Link target for `uri` from a page whose root prefix is `up`: relative and without ".html"
    for our URIs (as Pages serves them), the IRI itself for everything else."""
    s = str(uri)
    path = page_path(s)
    if path is None:
        return s
    if path == "ontology.html":
        return up + "ontology" + s[len(ONTOLOGY):]
    return up + path[:-len(".html")]


def compact(iri: str, used: set[str] | None = None) -> str:
    for prefix, ns in PREFIXES.items():
        if iri.startswith(ns) and len(iri) > len(ns):
            if used is not None:
                used.add(prefix)
            return f"{prefix}:{iri[len(ns):]}"
    return iri


# ---------------------------------------------------------------- loading

def load(inputs: dict[str, Path]) -> Triples:
    out: Triples = defaultdict(list)
    for name, path in inputs.items():
        t = time.time()
        g = Graph()
        g.parse(path, format="turtle")
        inferred = name == "inferred"
        for s, p, o in g:
            if isinstance(s, URIRef) and page_path(s) not in (None, "ontology.html"):
                out[s].append((p, o, inferred))
        print(f"  {name}: {len(g):,} triples ({time.time() - t:.1f} s)", flush=True)
    return out


def make_labels(out: Triples) -> dict[URIRef, str]:
    labels: dict[URIRef, str] = {}
    for prop in (RDFS.label, DCTERMS.title):
        for s, triples in out.items():
            if s not in labels:
                labels.update((s, str(o)) for p, o, inf in triples if p == prop and not inf)
    # Editions have no rdfs:label: name them after their book and ISBN (or Goodreads ID).
    for book, triples in out.items():
        for p, edition, _ in triples:
            if p == BOOKS.hasEdition and edition not in labels and book in labels:
                ids = {q: str(o) for q, o, _ in out.get(edition, ()) if q in (BOOKS.isbn, BOOKS.bookId)}
                ident = ids.get(BOOKS.isbn) or ids.get(BOOKS.bookId)
                labels[edition] = f"{labels[book]} (edition {ident})" if ident else f"{labels[book]} (edition)"
    return labels


def incoming(out: Triples) -> dict[URIRef, list[tuple[URIRef, URIRef]]]:
    """Asserted links pointing at each page's resource. Inferred ones are left out: they mirror
    asserted triples (inverses, schema.org and Dublin Core super-properties)."""
    inc: dict[URIRef, list[tuple[URIRef, URIRef]]] = defaultdict(list)
    for s, triples in out.items():
        for p, o, inf in triples:
            if not inf and p != RDF.type and isinstance(o, URIRef) and o in out:
                inc[o].append((s, p))
    return inc


# ---------------------------------------------------------------- rendering

class Renderer:
    def __init__(self, out: Triples, labels: dict[URIRef, str], inc, endpoint: str | None):
        self.out, self.labels, self.inc, self.endpoint = out, labels, inc, endpoint

    def label(self, node) -> str:
        if isinstance(node, URIRef):
            if node in self.labels:
                return self.labels[node]
            path = page_path(node)
            return compact(str(node)) if path in (None, "ontology.html") else str(node).rsplit("/", 1)[-1]
        return str(node)

    def term(self, node, up: str) -> str:
        if isinstance(node, BNode):
            return "(blank node)"
        if isinstance(node, Literal):
            text = esc(str(node))
            if node.datatype == XSD.anyURI:
                return f'<a href="{text}">{text}</a>'
            if node.language:
                return f"{text} <small>@{esc(node.language)}</small>"
            if node.datatype and node.datatype != XSD.string:
                return f"{text} <small>{esc(compact(str(node.datatype)))}</small>"
            return text
        return f'<a href="{esc(href(node, up))}">{esc(self.label(node))}</a>'

    def head(self, title: str, up: str, ld: str) -> str:
        return (f'<!doctype html><html lang=en><meta charset=utf-8>'
                f'<meta name=viewport content="width=device-width,initial-scale=1">'
                f'<title>{esc(title)}</title><link rel=stylesheet href="{up}style.css">'
                f'<script type="application/ld+json">{ld}</script>')

    def footer(self, uri: str | None) -> str:
        parts = [f'<a href="{REPO}">Source code</a>',
                 f'<a href="{REPO}/releases/tag/data-v1">Data dumps</a>',
                 '<a href="https://creativecommons.org/licenses/by-nc/4.0/">CC BY-NC 4.0</a>']
        if self.endpoint and uri:
            query = quote(f"DESCRIBE <{uri}>", safe="")
            parts.insert(0, f'<a href="{esc(self.endpoint)}?query={query}">DESCRIBE in the SPARQL endpoint</a> '
                            f'({esc(self.endpoint)}, runs locally, see the README)')
        return "<footer>" + " · ".join(parts) + "</footer>"

    def resource(self, uri: URIRef) -> str:
        path = page_path(uri)
        up = root_prefix(path)
        triples = self.out[uri]
        linked = {o for _, o, _ in triples}
        refs = [(s, p) for s, p in self.inc.get(uri, ()) if s not in linked]

        nodes: dict = {uri: [(p, o) for p, o, _ in triples]}
        for s, p in refs:
            nodes.setdefault(s, []).append((p, uri))
        title = self.label(uri)

        by_pred: dict = defaultdict(list)
        for p, o, inf in triples:
            by_pred[p].append((o, inf))
        rows = []
        for p in sorted(by_pred, key=predicate_order):
            values = sorted(by_pred[p], key=lambda v: self.label(v[0]).casefold())
            cell = "<br>".join(f"<em>{self.term(o, up)}</em>" if inf else self.term(o, up) for o, inf in values)
            rows.append(f"<tr><th>{self.term(p, up)}<td>{cell}")
        if refs:
            by_pred = defaultdict(list)
            for s, p in refs:
                by_pred[p].append(s)
            rows.append("<tr><th colspan=2><h3>Referenced by</h3>")
            for p in sorted(by_pred, key=predicate_order):
                subjects = sorted(by_pred[p], key=lambda s: self.label(s).casefold())
                rows.append(f"<tr><th>is {self.term(p, up)} of<td>" + "<br>".join(self.term(s, up) for s in subjects))

        kind = path.split("/")[1] if path.startswith("resource/") else ""
        nav = f'<nav><a href="{up}">Books Linked Data</a> · <a href="{up}ontology">Ontology</a>' + (f" · {kind}" if kind else "") + "</nav>"
        note = ('<p class=note>Italic values were inferred by the OWL 2 RL reasoner (reasoning/materialize.py).</p>'
                if any(inf for _, _, inf in triples) else "")
        return (self.head(title, up, jsonld(nodes)) + nav + f"<h1>{esc(title)}</h1><p class=uri>{esc(str(uri))}</p>"
                + note + "<table>" + "".join(rows) + "</table>" + self.footer(str(uri)))


def predicate_order(p: URIRef) -> tuple[int, str]:
    first = {RDF.type: 0, RDFS.label: 1, OWL.sameAs: 2}
    q = compact(str(p))
    return first.get(p, 3 if q.startswith("books:") else 4), q


def jsonld(nodes: dict) -> str:
    """JSON-LD for {subject: [(predicate, object)]}, safe to embed in a <script> element."""
    used: set[str] = set()

    def value(o):
        if isinstance(o, URIRef):
            return {"@id": compact(str(o), used)}
        if isinstance(o, BNode):
            return {"@id": f"_:{o}"}
        if o.language:
            return {"@value": str(o), "@language": o.language}
        if o.datatype:
            return {"@value": str(o), "@type": compact(str(o.datatype), used)}
        return str(o)

    graph = []
    for s, pairs in nodes.items():
        node: dict = {"@id": compact(str(s), used)}
        for p, o in pairs:
            node.setdefault(compact(str(p), used), []).append(value(o))
        graph.append(node)
    doc = {"@context": {k: PREFIXES[k] for k in sorted(used)}, "@graph": graph}
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


# ---------------------------------------------------------------- ontology page

CARDINALITIES = [(OWL.qualifiedCardinality, "exactly"), (OWL.minQualifiedCardinality, "min"),
                 (OWL.maxQualifiedCardinality, "max"), (OWL.cardinality, "exactly"),
                 (OWL.minCardinality, "min"), (OWL.maxCardinality, "max")]


def render_ontology(g: Graph, r: Renderer) -> str:
    up = "./"

    def link(node) -> str:
        return expr(node) if isinstance(node, BNode) else f'<a href="{esc(href(node, up))}">{esc(compact(str(node)))}</a>'

    def expr(node) -> str:  # class expressions in Manchester syntax
        if not isinstance(node, BNode):
            return link(node)
        prop = g.value(node, OWL.onProperty)
        if prop is not None:
            for pred, word in [(OWL.someValuesFrom, "some"), (OWL.allValuesFrom, "only"), (OWL.hasValue, "value")]:
                filler = g.value(node, pred)
                if filler is not None:
                    return f"{link(prop)} {word} {link(filler)}"
            for pred, word in CARDINALITIES:
                n = g.value(node, pred)
                if n is not None:
                    on = g.value(node, OWL.onClass) or g.value(node, OWL.onDataRange)
                    return f"{link(prop)} {word} {n}" + (f" {link(on)}" if on is not None else "")
        for pred, word in [(OWL.intersectionOf, " and "), (OWL.unionOf, " or ")]:
            items = g.value(node, pred)
            if items is not None:
                return "(" + word.join(expr(x) for x in Collection(g, items)) + ")"
        if (c := g.value(node, OWL.complementOf)) is not None:
            return f"not {expr(c)}"
        return "(blank node)"

    def named(kind) -> list[URIRef]:
        return sorted((s for s in g.subjects(RDF.type, kind) if isinstance(s, URIRef)), key=str)

    disjoint_groups = [list(Collection(g, m)) for d in g.subjects(RDF.type, OWL.AllDisjointClasses)
                       for m in g.objects(d, OWL.members)]

    def section(term: URIRef, rows: list[tuple[str, list[str]]]) -> str:
        local = str(term)[len(str(BOOKS)):]
        body = "".join(f"<tr><th>{name}<td>{'<br>'.join(vals)}" for name, vals in rows if vals)
        comments = "".join(f"<p>{esc(str(c))}</p>" for c in g.objects(term, RDFS.comment))
        labels = "".join(f"<p>{esc(str(c))}</p>" for c in g.objects(term, RDFS.label))
        return (f'<section id="{esc(local)}"><h3>{esc(local)}</h3><p class=uri>{esc(str(term))}</p>'
                f"{labels}{comments}<table>{body}</table></section>")

    def ours(terms) -> list[URIRef]:
        return [t for t in terms if str(t).startswith(str(BOOKS))]

    classes = ours(named(OWL.Class))
    object_props, data_props = ours(named(OWL.ObjectProperty)), ours(named(OWL.DatatypeProperty))
    class_sections = []
    for c in classes:
        disjoint = {o for o in g.objects(c, OWL.disjointWith)} | {s for s in g.subjects(OWL.disjointWith, c)}
        disjoint |= {m for group in disjoint_groups if c in group for m in group if m != c}
        class_sections.append(section(c, [
            ("Subclass of", [expr(o) for o in g.objects(c, RDFS.subClassOf)]),
            ("Equivalent to", [expr(o) for o in g.objects(c, OWL.equivalentClass)]),
            ("Disjoint with", [link(o) for o in sorted(disjoint, key=str)]),
            ("Subclasses", [link(s) for s in sorted(g.subjects(RDFS.subClassOf, c), key=str) if isinstance(s, URIRef)]),
            ("Domain of", [link(p) for p in sorted(g.subjects(RDFS.domain, c), key=str)]),
            ("Range of", [link(p) for p in sorted(g.subjects(RDFS.range, c), key=str)]),
        ]))
    prop_sections = {}
    for kind, props in [("object", object_props), ("data", data_props)]:
        prop_sections[kind] = [section(p, [
            ("Type", [link(t) for t in sorted(g.objects(p, RDF.type), key=str)]),
            ("Domain", [expr(o) for o in g.objects(p, RDFS.domain)]),
            ("Range", [expr(o) for o in g.objects(p, RDFS.range)]),
            ("Subproperty of", [link(o) for o in g.objects(p, RDFS.subPropertyOf)]),
            ("Subproperties", [link(s) for s in sorted(g.subjects(RDFS.subPropertyOf, p), key=str)]),
            ("Inverse of", [link(o) for o in sorted(set(g.objects(p, OWL.inverseOf)) | set(g.subjects(OWL.inverseOf, p)), key=str)]),
        ]) for p in props]

    onto = URIRef(ONTOLOGY)
    title = str(g.value(onto, RDFS.label) or "Books ontology")
    toc = "".join(f"<p><b>{name}:</b> " + ", ".join(f'<a href="#{esc(str(t)[len(str(BOOKS)):])}">{esc(str(t)[len(str(BOOKS)):])}</a>' for t in terms) + "</p>"
                  for name, terms in [("Classes", classes), ("Object properties", object_props), ("Datatype properties", data_props)])
    ld = g.serialize(format="json-ld").replace("</", "<\\/")
    return (r.head(title, up, ld) + f'<nav><a href="{up}">Books Linked Data</a> · Ontology</nav>'
            f"<h1>{esc(title)}</h1><p class=uri>{esc(ONTOLOGY)}</p>"
            + "".join(f"<p>{esc(str(c))}</p>" for c in g.objects(onto, RDFS.comment))
            + f'<p>Prefix <code>books:</code> = <code>{esc(str(BOOKS))}</code>. Turtle: <a href="ontology.ttl">ontology.ttl</a>.</p>'
            + toc + "<h2>Classes</h2>" + "".join(class_sections)
            + "<h2>Object properties</h2>" + "".join(prop_sections["object"])
            + "<h2>Datatype properties</h2>" + "".join(prop_sections["data"]) + r.footer(None))


# ---------------------------------------------------------------- index and 404

def render_index(r: Renderer) -> str:
    up = "./"
    meta = defaultdict(list)
    for p, o, _ in r.out.get(DATASET, ()):
        meta[p].append(o)
    title = str(meta[DCTERMS.title][0]) if meta[DCTERMS.title] else "Books Linked Data"
    description = "".join(f"<p>{esc(str(d))}</p>" for d in meta[DCTERMS.description])

    by_kind: dict[str, list[URIRef]] = defaultdict(list)
    for s in r.out:
        path = page_path(s)
        if path.startswith("resource/"):
            by_kind[path.split("/")[1]].append(s)
    rows = []
    for kind, subjects in sorted(by_kind.items(), key=lambda kv: -len(kv[1])):
        example = max(subjects, key=lambda s: (len(r.inc.get(s, ())), len(r.out[s]), str(s)))
        rows.append(f"<tr><th>{esc(kind)}<td>{len(subjects):,}<td>{r.term(example, up)}")

    def ratings(s) -> int:
        return next((int(o) for p, o, _ in r.out[s] if p == BOOKS.numRatings), 0)

    top = sorted(by_kind.get("book", []), key=ratings, reverse=True)[:10]
    dumps = "".join(f'<li><a href="{esc(str(d))}">{esc(str(d).rsplit("/", 1)[-1])}</a>' for d in sorted(meta[VOID.dataDump], key=str))
    endpoint = (f"<li>SPARQL endpoint: <code>{esc(r.endpoint)}</code> (Apache Jena Fuseki; it runs on our machine, "
                f'see the <a href="{REPO}#readme">README</a>)' if r.endpoint else "")
    links = (f'<ul><li>Ontology: <a href="ontology">HTML</a> · <a href="ontology.ttl">Turtle</a>'
             f'<li>Dataset description (VoID): <a href="dataset">HTML</a> · <a href="void.ttl">Turtle</a>'
             f"{endpoint}<li>Source code and documentation: <a href=\"{REPO}\">{REPO}</a>"
             f'<li>Licence: <a href="https://creativecommons.org/licenses/by-nc/4.0/">CC BY-NC 4.0</a>, '
             f'derived from the <a href="https://doi.org/10.5281/zenodo.4265096">Best Books Ever dataset</a> (Zenodo)</ul>')
    return (r.head(title, up, jsonld({DATASET: [(p, o) for p, o, _ in r.out.get(DATASET, ())]}))
            + f"<h1>{esc(title)}</h1>{description}{links}"
            + f"<h2>Data dumps</h2><p>GitHub release <a href=\"{REPO}/releases/tag/data-v1\">data-v1</a>, one file per named graph:</p><ul>{dumps}</ul>"
            + "<h2>Resources</h2><p>Every URI below <code>" + esc(RESOURCE) + "</code> has a page like these.</p>"
            + "<table><tr><th>Kind<td>Pages<td>Example" + "".join(rows) + "</table>"
            + "<h2>Most-rated books</h2><ol>" + "".join(f"<li>{r.term(b, up)}" for b in top) + "</ol>"
            + r.footer(None))


def render_404() -> str:
    root = urlsplit(BASE).path  # absolute: Pages serves this page at any missing path
    return (f'<!doctype html><html lang=en><meta charset=utf-8><title>Not found</title>'
            f'<link rel=stylesheet href="{root}style.css"><h1>Not found</h1>'
            f'<p>No resource has this URI. Start at <a href="{root}">Books Linked Data</a>.</p>')


# ---------------------------------------------------------------- build / serve

def build(site: Path) -> None:
    t0 = time.time()
    missing = [p for p in [*INPUTS.values(), ONTOLOGY_FILE] if not p.exists()]
    if missing:
        sys.exit("missing " + ", ".join(str(p.relative_to(ROOT)) for p in missing)
                 + ": run the pipeline and reasoning/materialize.py, or download release data-v1 (README)")
    print("loading", flush=True)
    out = load(INPUTS)
    labels, inc = make_labels(out), incoming(out)
    endpoint = next((str(o) for p, o, _ in out.get(DATASET, ()) if p == VOID.sparqlEndpoint), None)
    r = Renderer(out, labels, inc, endpoint)
    ontology = Graph()
    ontology.parse(ONTOLOGY_FILE, format="turtle")

    if site.exists():
        if not (site / ".nojekyll").exists():
            sys.exit(f"{site} exists but is not a generated site (no .nojekyll); refusing to delete it")
        shutil.rmtree(site)
    site.mkdir(parents=True)
    t1 = time.time()
    made: set[Path] = set()
    for uri in out:
        file = site / page_path(uri)
        if file.parent not in made:
            file.parent.mkdir(parents=True, exist_ok=True)
            made.add(file.parent)
        file.write_text(r.resource(uri), encoding="utf-8")
    (site / "ontology.html").write_text(render_ontology(ontology, r), encoding="utf-8")
    (site / "index.html").write_text(render_index(r), encoding="utf-8")
    (site / "404.html").write_text(render_404(), encoding="utf-8")
    (site / "style.css").write_text(CSS, encoding="utf-8")
    (site / ".nojekyll").write_text("", encoding="utf-8")  # no Jekyll run over ~77k files
    shutil.copyfile(ONTOLOGY_FILE, site / "ontology.ttl")
    shutil.copyfile(INPUTS["metadata"], site / "void.ttl")
    size = sum(f.stat().st_size for f in site.rglob("*") if f.is_file())
    print(f"{len(out):,} resource pages, {size / 2**20:,.0f} MiB in {site} "
          f"(load {t1 - t0:.0f} s, write {time.time() - t1:.0f} s)")


def serve(site: Path, port: int) -> None:
    """Serve `site` like GitHub Pages: a request for /x gets x.html when x itself does not exist."""
    class PagesHandler(SimpleHTTPRequestHandler):
        def translate_path(self, path: str) -> str:
            local = super().translate_path(path)
            return local + ".html" if not os.path.exists(local) and os.path.exists(local + ".html") else local

    print(f"serving {site} at http://localhost:{port}/")
    ThreadingHTTPServer(("127.0.0.1", port), partial(PagesHandler, directory=str(site))).serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=ROOT / "site", help="output directory (default: site/)")
    parser.add_argument("--serve", type=int, metavar="PORT", help="preview an already built site instead of building")
    args = parser.parse_args()
    if args.serve:
        serve(args.out, args.serve)
    else:
        build(args.out)


if __name__ == "__main__":
    main()
