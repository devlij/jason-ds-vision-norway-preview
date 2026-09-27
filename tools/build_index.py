#!/usr/bin/env python3
"""Durable Norway gallery publisher for verbose scene descriptions.

The kit file tools/scene_descriptions.json is the source of truth (verbatim
entry_id → description text). data.json is the scene catalogue. index.html
holds the published cards and the SCENES array.

Running this script rewrites each matching scene's description into:

  - data.json (catalogue field, after composition)
  - the card <p class="description"> and its data-search haystack
  - the SCENES object embedded in index.html

Scenes absent from the kit are left unchanged. Description strings are copied
through untouched. Image files are never written. A second run is a no-op,
so a later publisher rebuild that starts from this script keeps the texts.

    python3 tools/build_index.py
    python3 tools/build_index.py --prove
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KIT = Path(__file__).resolve().parent / "scene_descriptions.json"
CATALOGUE = ROOT / "data.json"
INDEX = ROOT / "index.html"

ARTICLE_RE = re.compile(r'<article class="card" id="(NO-\d+-\d+)"')
DESC_RE = re.compile(r"(<p class=\"description\">)(.*?)(</p>)", re.S)
SEARCH_RE = re.compile(r'data-search="[^"]*"')
ENTRY_RE = re.compile(r"^NO-\d+-\d+$")


def load_kit() -> dict[str, str]:
    data = json.loads(KIT.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"{KIT.name} must be a JSON object")
    cleaned: dict[str, str] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not ENTRY_RE.match(key):
            raise SystemExit(f"kit key is not an entry id: {key!r}")
        if not isinstance(value, str) or value == "":
            raise SystemExit(f"{key} description must be a non-empty string")
        cleaned[key] = value
    if len(cleaned) != len(data):
        raise SystemExit("kit has duplicate entry ids")
    return cleaned


def js_span(page: str, marker: str, open_ch: str, close_ch: str) -> tuple[int, int]:
    start = page.index(marker)
    i = page.index(open_ch, start)
    depth = 0
    in_str = False
    esc = False
    for j in range(i, len(page)):
        c = page[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i, j + 1
    raise SystemExit(f"unclosed {open_ch} after {marker}")


def with_description(scene: dict, text: str) -> dict:
    """Place description immediately after composition. Leave every other field."""
    if "composition" not in scene:
        updated = dict(scene)
        updated["description"] = text
        return updated
    updated: dict = {}
    for key, value in scene.items():
        if key == "description":
            continue
        updated[key] = value
        if key == "composition":
            updated["description"] = text
    return updated


def apply_catalogue(scenes: list[dict], kit: dict[str, str]) -> list[dict]:
    out: list[dict] = []
    for scene in scenes:
        entry_id = scene.get("entry_id")
        if entry_id in kit:
            out.append(with_description(scene, kit[entry_id]))
        else:
            out.append(scene)
    return out


def catalogue_text(scenes: list[dict]) -> str:
    # data.json has no trailing newline; keep that so an unchanged catalogue
    # stays byte-identical and a rewritten one stays stable across runs.
    return json.dumps(scenes, indent=2, ensure_ascii=False)


def search_value(scene: dict, text: str) -> str:
    entry_id = scene["entry_id"]
    prefix = f"{scene['city']} {scene['site']} {scene['region']} {entry_id}".lower()
    return html.escape(prefix + " " + text.lower(), quote=True)


def rewrite_article(block: str, scene: dict, text: str) -> str:
    escaped = html.escape(text, quote=True)
    updated, n = DESC_RE.subn(lambda m: m.group(1) + escaped + m.group(3), block, count=1)
    if n != 1:
        composition = re.search(r"(<p class=\"composition\">.*?</p>)", block, re.S)
        if not composition:
            raise SystemExit(f"{scene['entry_id']} has no description or composition paragraph")
        insert_at = composition.end()
        updated = (
            block[:insert_at]
            + f'\n            <p class="description">{escaped}</p>'
            + block[insert_at:]
        )
    updated, n = SEARCH_RE.subn(
        f'data-search="{search_value(scene, text)}"',
        updated,
        count=1,
    )
    if n != 1:
        raise SystemExit(f"{scene['entry_id']} card is missing data-search")
    return updated


def apply_cards(page: str, catalogue: dict[str, dict], kit: dict[str, str]) -> str:
    matches = list(ARTICLE_RE.finditer(page))
    if not matches:
        raise SystemExit("index.html has no scene cards")
    pieces: list[str] = []
    cursor = 0
    seen: set[str] = set()
    for index, match in enumerate(matches):
        entry_id = match.group(1)
        if entry_id in seen:
            raise SystemExit(f"duplicate card {entry_id}")
        seen.add(entry_id)
        start = match.start()
        if index + 1 < len(matches):
            end = matches[index + 1].start()
        else:
            close = page.find("</article>", start)
            if close < 0:
                raise SystemExit(f"{entry_id} card is not closed")
            end = close + len("</article>")
        pieces.append(page[cursor:start])
        block = page[start:end]
        if entry_id in kit:
            if entry_id not in catalogue:
                raise SystemExit(f"{entry_id} is in the kit but not in data.json")
            block = rewrite_article(block, catalogue[entry_id], kit[entry_id])
        pieces.append(block)
        cursor = end
    pieces.append(page[cursor:])
    return "".join(pieces)


def apply_scenes(page: str, kit: dict[str, str]) -> str:
    start, end = js_span(page, "const SCENES = ", "[", "]")
    scenes = json.loads(page[start:end])
    if not isinstance(scenes, list):
        raise SystemExit("SCENES must be an array")
    for scene in scenes:
        entry_id = scene.get("entry_id")
        if entry_id in kit:
            scene["description"] = kit[entry_id]
    rendered = json.dumps(scenes, indent=2, ensure_ascii=False)
    return page[:start] + rendered + page[end:]


def build(kit: dict[str, str], catalogue: list[dict], page: str) -> tuple[list[dict], str]:
    updated_catalogue = apply_catalogue(catalogue, kit)
    by_id = {scene["entry_id"]: scene for scene in updated_catalogue}
    if len(by_id) != len(updated_catalogue):
        raise SystemExit("data.json has duplicate entry ids")
    updated_page = apply_cards(page, by_id, kit)
    updated_page = apply_scenes(updated_page, kit)
    return updated_catalogue, updated_page


def card_descriptions(page: str) -> dict[str, str]:
    found: dict[str, str] = {}
    matches = list(ARTICLE_RE.finditer(page))
    for index, match in enumerate(matches):
        entry_id = match.group(1)
        start = match.start()
        if index + 1 < len(matches):
            end = matches[index + 1].start()
        else:
            end = page.find("</article>", start) + len("</article>")
        block = page[start:end]
        parts = DESC_RE.findall(block)
        if len(parts) != 1:
            raise SystemExit(f"{entry_id} has {len(parts)} description paragraphs")
        if entry_id in found:
            raise SystemExit(f"duplicate card {entry_id}")
        found[entry_id] = html.unescape(parts[0][1])
    return found


def scene_descriptions(page: str) -> dict[str, str]:
    start, end = js_span(page, "const SCENES = ", "[", "]")
    scenes = json.loads(page[start:end])
    found: dict[str, str] = {}
    for scene in scenes:
        entry_id = scene["entry_id"]
        if entry_id in found:
            raise SystemExit(f"duplicate SCENES entry {entry_id}")
        if "description" in scene:
            found[entry_id] = scene["description"]
    return found


def prove(kit: dict[str, str], catalogue: list[dict], page: str) -> None:
    by_id = {scene["entry_id"]: scene for scene in catalogue}
    cards = card_descriptions(page)
    scenes = scene_descriptions(page)
    missing = []
    mismatched = []
    for entry_id, text in kit.items():
        surfaces = {
            "data.json": by_id.get(entry_id, {}).get("description"),
            "card": cards.get(entry_id),
            "SCENES": scenes.get(entry_id),
        }
        for surface, value in surfaces.items():
            if value is None:
                missing.append(f"{entry_id} missing on {surface}")
            elif value != text:
                mismatched.append(f"{entry_id} {surface} text differs")
    extra_cards = sorted(set(cards) - set(kit))
    # Cards not in the kit may keep whatever description they already had.
    if missing or mismatched:
        report = "\n".join(missing + mismatched)
        raise SystemExit(f"description proof failed:\n{report}")
    if len(kit) != 320:
        raise SystemExit(f"expected 320 kit entries, found {len(kit)}")
    print(f"proof: {len(kit)}/{len(kit)} ids present once with exact text")
    print(f"proof: cards={len(cards)} scenes={len(scenes)} catalogue={len(by_id)} extras_outside_kit={len(extra_cards)}")


def stripped_for_rebuild(catalogue: list[dict], page: str, kit: dict[str, str]) -> tuple[list[dict], str]:
    """Drop kit descriptions so a rebuild has to write them back."""
    bare_catalogue = []
    for scene in catalogue:
        if scene.get("entry_id") in kit and "description" in scene:
            bare = {key: value for key, value in scene.items() if key != "description"}
            bare_catalogue.append(bare)
        else:
            bare_catalogue.append(scene)
    bare_page = page
    start, end = js_span(bare_page, "const SCENES = ", "[", "]")
    scenes = json.loads(bare_page[start:end])
    for scene in scenes:
        if scene.get("entry_id") in kit:
            scene.pop("description", None)
    bare_page = bare_page[:start] + json.dumps(scenes, indent=2, ensure_ascii=False) + bare_page[end:]
    matches = list(ARTICLE_RE.finditer(bare_page))
    pieces: list[str] = []
    cursor = 0
    for index, match in enumerate(matches):
        entry_id = match.group(1)
        start_i = match.start()
        if index + 1 < len(matches):
            end_i = matches[index + 1].start()
        else:
            end_i = bare_page.find("</article>", start_i) + len("</article>")
        pieces.append(bare_page[cursor:start_i])
        block = bare_page[start_i:end_i]
        if entry_id in kit:
            block, removed = re.subn(
                r"\n[ \t]*<p class=\"description\">.*?</p>",
                "",
                block,
                count=1,
                flags=re.S,
            )
            if removed != 1:
                raise SystemExit(f"could not strip description from {entry_id}")
        pieces.append(block)
        cursor = end_i
    pieces.append(bare_page[cursor:])
    return bare_catalogue, "".join(pieces)


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish Norway scene descriptions from the kit.")
    parser.add_argument("--prove", action="store_true", help="Check exact texts and rebuild persistence.")
    args = parser.parse_args()

    kit = load_kit()
    catalogue = json.loads(CATALOGUE.read_text(encoding="utf-8"))
    page = INDEX.read_text(encoding="utf-8")
    if not isinstance(catalogue, list):
        raise SystemExit("data.json must be an array")

    updated_catalogue, updated_page = build(kit, catalogue, page)
    catalogue_out = catalogue_text(updated_catalogue)
    wrote = False
    if catalogue_out != CATALOGUE.read_text(encoding="utf-8"):
        CATALOGUE.write_text(catalogue_out, encoding="utf-8")
        wrote = True
    if updated_page != page:
        INDEX.write_text(updated_page, encoding="utf-8")
        wrote = True
    print("wrote catalogue and index" if wrote else "already up to date")

    if args.prove:
        prove(kit, updated_catalogue, updated_page)
        again_catalogue, again_page = build(kit, updated_catalogue, updated_page)
        if catalogue_text(again_catalogue) != catalogue_out or again_page != updated_page:
            raise SystemExit("second publish changed files; generator is not stable")
        bare_catalogue, bare_page = stripped_for_rebuild(updated_catalogue, updated_page, kit)
        rebuilt_catalogue, rebuilt_page = build(kit, bare_catalogue, bare_page)
        if catalogue_text(rebuilt_catalogue) != catalogue_out or rebuilt_page != updated_page:
            raise SystemExit("rebuild from a description-free page did not restore the kit texts")
        prove(kit, rebuilt_catalogue, rebuilt_page)
        print("proof: regenerate restored every description; second run was a no-op")
    return 0


if __name__ == "__main__":
    sys.exit(main())
