"""URI -> file mapping of the GitHub Pages site (publish/build_pages.py)."""

import sys
from pathlib import Path
from urllib.parse import urljoin

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_pages import BASE, href, page_path, root_prefix  # noqa: E402

R = BASE + "resource/"
HUNGER_GAMES = R + "book/the-hunger-games--4966d945b5bd060f"
SVEJK = R + "book/the-good-soldier-švejk--0220c05aa8cc81eb"


@pytest.mark.parametrize("uri, path", [
    (HUNGER_GAMES, "resource/book/the-hunger-games--4966d945b5bd060f.html"),
    (SVEJK, "resource/book/the-good-soldier-švejk--0220c05aa8cc81eb.html"),
    (BASE + "dataset", "dataset.html"),
    (BASE + "linkset/wikidata", "linkset/wikidata.html"),
    (BASE + "ontology", "ontology.html"),
    (BASE + "ontology#Book", "ontology.html"),
])
def test_our_uris_map_to_the_file_pages_serves(uri, path):
    assert page_path(uri) == path


@pytest.mark.parametrize("uri", [
    "http://www.wikidata.org/entity/Q1052268",
    "http://shao2011.github.io/Proj-SemWeb/resource/book/x--1",  # http, not our https namespace
    BASE, R, R + "book/",                                        # directories
    BASE + "ontology.ttl",                                       # a file next to ontology.html
    R + "book/../../../etc/passwd", R + "book//x", R + "book/./x",
    R + "book/x?y=1", R + "book/x#y",
])
def test_other_uris_have_no_page(uri):
    assert page_path(uri) is None


@pytest.mark.parametrize("page", [HUNGER_GAMES, SVEJK, BASE + "dataset", BASE + "linkset/wikidata"])
@pytest.mark.parametrize("target", [HUNGER_GAMES, SVEJK, R + "person/suzanne-collins--1", BASE + "dataset",
                                    BASE + "ontology#Book", BASE + "ontology", "http://www.wikidata.org/entity/Q1"])
def test_links_resolve_to_the_target_uri_from_any_page(page, target):
    # A browser on the page's URI (Pages serves it without ".html") resolves the relative link.
    assert urljoin(page, href(target, root_prefix(page_path(page)))) == target
