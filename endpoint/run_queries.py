#!/usr/bin/env python3
"""Run every endpoint/queries/*.rq against the Fuseki endpoint.

Prints the competency question, row count, time and the first rows, and saves the full
result of each query as CSV in endpoint/results/.
Usage: python endpoint/run_queries.py [--endpoint URL] [--show N] [names...]
"""

import argparse
import csv
import io
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


def run(endpoint: str, query: str) -> list[list[str]]:
    request = urllib.request.Request(endpoint, data=urllib.parse.urlencode({"query": query}).encode(),
                                     headers={"Accept": "text/csv"})
    with urllib.request.urlopen(request, timeout=300) as response:
        return list(csv.reader(io.StringIO(response.read().decode("utf-8"))))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--endpoint", default="http://localhost:3030/books/sparql")
    parser.add_argument("--show", type=int, default=5, help="rows to print per query")
    parser.add_argument("names", nargs="*", help="query names or prefixes, e.g. cq03 (default: all)")
    args = parser.parse_args()
    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    failed = 0
    for path in sorted((HERE / "queries").glob("*.rq")):
        if args.names and not any(path.stem.startswith(n.removesuffix(".rq")) for n in args.names): continue
        query = path.read_text(encoding="utf-8")
        question = next((l[2:] for l in query.splitlines() if l.startswith("# ")), path.stem)
        started = time.time()
        try:
            rows = run(args.endpoint, query)
        except Exception as exc:  # report and continue with the other queries
            print(f"\n== {path.stem}: FAILED {exc}")
            failed += 1
            continue
        with (out_dir / f"{path.stem}.csv").open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerows(rows)
        print(f"\n== {path.stem} ({len(rows) - 1} rows, {time.time() - started:.1f} s)\n   {question}")
        for row in rows[:args.show + 1]: print("   " + " | ".join(cell[:60] for cell in row))
    sys.exit(1 if failed else 0)


if __name__ == "__main__": main()
