#!/usr/bin/env python3
"""Pull the day's Wordle and Connections solutions into the mod.

Driven by .github/workflows/solution-update.yml, but runnable by hand:

    python3 tools/update_solutions.py                    # today, UTC
    python3 tools/update_solutions.py --date 2026-09-20
    python3 tools/update_solutions.py --dry-run

What it does:

  1. Fetches the Wordle answer from the NYT puzzle service.
  2. Fetches the Connections categories from the NYT puzzle service.
  3. Rewrites main_menu/localization/english/wu_solutions_l_english.yml with
     the new keys, replacing the previous day's.
  4. Rewrites the "version" value in .metadata/metadata.json to YY.MMDD.

Standard library only - nothing to install on the runner.

Both endpoints are keyed by print date and are populated well ahead of time
(weeks, in practice), so the run does not have to wait for the midnight US
Eastern rollover - asking for a date that has not been played yet is fine.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

WORDLE_URL = "https://www.nytimes.com/svc/wordle/v2/{date}.json"
CONNECTIONS_URL = "https://www.nytimes.com/svc/connections/v2/{date}.json"

USER_AGENT = "wordle-universalis-solution-update/1.0 (+github-actions)"

LOC_PATH = "main_menu/localization/english/wu_solutions_l_english.yml"
METADATA_PATH = ".metadata/metadata.json"

# Matches the "version": "..." pair in metadata.json without touching byte-order
# marks, CRLF line endings or tab indentation elsewhere in the file.
VERSION_RE = re.compile(rb'("version"\s*:\s*")([^"]*)(")')


class SolutionError(RuntimeError):
    """Something went wrong that a human needs to look at."""


# --------------------------------------------------------------------------- #
# fetching
# --------------------------------------------------------------------------- #

def fetch_json(url: str, *, attempts: int = 4, backoff: float = 5.0):
    """GET a URL and parse it as JSON, retrying transient failures."""
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Cache-Control": "no-cache",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            last = error
            # A puzzle that is not in the service yet answers 404; no point
            # hammering it, and neither is any other 4xx worth a retry.
            if error.code == 404:
                raise SolutionError(f"{url} returned 404 (no puzzle for that date)") from error
            if error.code < 500 and error.code != 429:
                raise SolutionError(f"{url} returned HTTP {error.code}") from error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            last = error
        if attempt < attempts:
            delay = backoff * attempt
            print(f"  fetch failed ({last}); retrying in {delay:.0f}s", flush=True)
            time.sleep(delay)
    raise SolutionError(f"Could not fetch {url}: {last}")


def check_print_date(payload: dict, date: dt.date, what: str) -> None:
    """Refuse a payload that is not the day we asked for.

    The whole point of the date in the URL is that it selects the puzzle. If the
    service ever answers with a different print_date, publishing it would put the
    wrong day's answers in front of players, so stop instead.
    """
    printed = str(payload.get("print_date", "")).strip()
    if printed and printed != date.isoformat():
        raise SolutionError(
            f"{what} service was asked for {date} but answered with print_date "
            f"{printed!r}. Refusing to publish a different day's puzzle."
        )


def fetch_wordle(date: dt.date) -> dict:
    """Return the Wordle answer for `date`."""
    payload = fetch_json(WORDLE_URL.format(date=date.isoformat()))
    check_print_date(payload, date, "Wordle")

    solution = str(payload.get("solution", "")).strip()
    if not solution.isalpha():
        raise SolutionError(f"Wordle payload for {date} has no usable solution: {payload!r}")

    return {
        "solution": solution.upper(),
        "id": payload.get("days_since_launch", payload.get("id", "")),
    }


def fetch_connections(date: dt.date) -> dict:
    """Return the four Connections categories for `date`."""
    payload = fetch_json(CONNECTIONS_URL.format(date=date.isoformat()))

    status = str(payload.get("status", "OK")).strip()
    if status.upper() != "OK":
        raise SolutionError(f"Connections service returned status {status!r} for {date}.")

    check_print_date(payload, date, "Connections")

    # The service lists categories easiest-first (yellow, green, blue, purple)
    # and carries no numeric difficulty field, so the given order is the order.
    # Cards come alphabetically within a category; `position` (0-15) is the slot
    # on the official shuffled board and is not used here.
    categories = payload.get("categories")
    if not isinstance(categories, list) or len(categories) != 4:
        raise SolutionError(f"Connections payload for {date} does not have 4 categories: {payload!r}")

    groups = []
    for category in categories:
        title = str(category.get("title", "")).strip()
        cards = category.get("cards")
        if not title or not isinstance(cards, list) or len(cards) != 4:
            raise SolutionError(f"Malformed Connections category for {date}: {category!r}")

        members = [str(card.get("content", "")).strip() for card in cards]
        if not all(members):
            raise SolutionError(f"Empty card in Connections category {title!r} for {date}.")

        groups.append({"group": title, "members": members})

    return {"id": payload.get("id", ""), "groups": groups}


# --------------------------------------------------------------------------- #
# writing
# --------------------------------------------------------------------------- #

def escape(value: str) -> str:
    """Escape a value for a Paradox localization string."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def render_loc(date: dt.date, wordle: dict, connections: dict) -> str:
    """Build the whole wu_solutions_l_english.yml body."""
    out = [
        "l_english:",
        " # Generated by tools/update_solutions.py - do not edit by hand.",
        f" # Puzzle date: {date.isoformat()}",
        "",
        f' wu_solution_date: "{date.isoformat()}"',
        "",
        " # Wordle",
        f' wu_wordle_id: "{escape(wordle["id"])}"',
        f' wu_wordle_solution: "{escape(wordle["solution"])}"',
        "",
        " # Connections",
        f' wu_connections_id: "{escape(connections["id"])}"',
    ]

    for index, group in enumerate(connections["groups"], start=1):
        out.append("")
        out.append(f' wu_connections_cat_{index}: "{escape(group["group"])}"')
        for slot, member in enumerate(group["members"], start=1):
            out.append(f' wu_connections_cat_{index}_word_{slot}: "{escape(member)}"')

    out.append("")
    return "\n".join(out)


def write_loc(path: str, body: str) -> bool:
    """Write the localization file as UTF-8-BOM with LF endings. True if changed."""
    new = body.encode("utf-8")
    try:
        with open(path, "rb") as handle:
            old = handle.read()
    except FileNotFoundError:
        old = None

    payload = b"\xef\xbb\xbf" + new
    if old == payload:
        return False

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(payload)
    return True


def bump_metadata(path: str, version: str) -> tuple[str, bool]:
    """Replace the "version" value in metadata.json, leaving the rest byte-identical."""
    with open(path, "rb") as handle:
        raw = handle.read()

    match = VERSION_RE.search(raw)
    if not match:
        raise SolutionError(f'No "version" key found in {path}.')

    current = match.group(2).decode("utf-8")
    # Keep whatever prefix style the file already uses (e.g. "v26.0919").
    if current.startswith("v"):
        version = "v" + version

    if current == version:
        return version, False

    updated = VERSION_RE.sub(
        lambda _m: match.group(1) + version.encode("utf-8") + match.group(3),
        raw,
        count=1,
    )
    with open(path, "wb") as handle:
        handle.write(updated)
    return version, True


def emit_outputs(**values: object) -> None:
    """Append key=value pairs to $GITHUB_OUTPUT when running in Actions."""
    target = os.environ.get("GITHUB_OUTPUT")
    if not target:
        return
    with open(target, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--date",
        default="",
        help="Puzzle date as YYYY-MM-DD. Defaults to today's UTC date, which is the "
             "US Eastern puzzle day the run is reaching for.",
    )
    parser.add_argument(
        "--repo-root",
        default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        help="Repository root. Defaults to the parent of tools/.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written without touching any files.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.date:
        try:
            date = dt.date.fromisoformat(args.date)
        except ValueError:
            print(f"::error::--date must be YYYY-MM-DD, got {args.date!r}", file=sys.stderr)
            return 2
    else:
        date = dt.datetime.now(dt.timezone.utc).date()

    version = f"{date:%y}.{date:%m}{date:%d}"
    loc_path = os.path.join(args.repo_root, LOC_PATH)
    metadata_path = os.path.join(args.repo_root, METADATA_PATH)

    print(f"Puzzle date: {date.isoformat()}  ->  version {version}")

    try:
        print("Fetching Wordle...")
        wordle = fetch_wordle(date)
        print("Fetching Connections...")
        connections = fetch_connections(date)
    except SolutionError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1

    body = render_loc(date, wordle, connections)

    print()
    print(f"Wordle:      {wordle['solution']}")
    for index, group in enumerate(connections["groups"], start=1):
        print(f"Category {index}:  {group['group']}: {', '.join(group['members'])}")
    print()

    if args.dry_run:
        print("--dry-run: nothing written.\n")
        print(body)
        return 0

    try:
        loc_changed = write_loc(loc_path, body)
        version, metadata_changed = bump_metadata(metadata_path, version)
    except (SolutionError, OSError) as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1

    changed = loc_changed or metadata_changed
    print(f"{LOC_PATH}: {'updated' if loc_changed else 'unchanged'}")
    print(f"{METADATA_PATH}: {'version -> ' + version if metadata_changed else 'unchanged'}")

    emit_outputs(
        version=version,
        date=date.isoformat(),
        wordle=wordle["solution"],
        changed="true" if changed else "false",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
