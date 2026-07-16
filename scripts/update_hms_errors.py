"""Vendor Bambu's official HMS error-text catalog + wiki deep links.

Companion to ``update_hms_actions.py``. Where that script fetches the per-code
remediation *action* lists, this one fetches the human-readable fault *text* and
the per-code wiki troubleshooting URL, so the fork can decode any HMS code the
live fleet reports without a network round-trip at runtime.

Two data files are produced (gzipped JSON, eager-loaded by
``backend/app/services/hms_catalog.py``):

* ``backend/app/data/hms_error_text_en.json.gz`` — flat ``{ECODE_UPPER: intro}``.
  Keys are Bambu's ``ecode`` verbatim (uppercased): 16 hex chars for
  ``device_hms`` faults (attr<<32 | code) and 8 hex chars for ``device_error``
  faults. The two key lengths are disjoint, so one flat dict is unambiguous and
  matches the ``full_code`` the MQTT parser builds
  (``bambu_mqtt.py``: ``f"{attr:08X}{code:08X}"`` / ``f"{print_error:08X}"``).

* ``backend/app/data/hms_wiki_links_en.json.gz`` — ``{ECODE16_UPPER: path}`` where
  path is the wiki troubleshooting deep link
  ``/en/<device>/troubleshooting/hmscode/XXXX_XXXX_XXXX_XXXX``. Built by scraping
  the HMS home index then HEAD-probing constructed ``/en/x1`` then ``/en/h2``
  paths for codes the scrape did not cover. Probe hits AND misses are persisted
  to ``scripts/hms_wiki_probe_cache.json`` so reruns never re-probe.

Source endpoints (verified 2026-07-12):
  * ``https://e.bambulab.com/query.php?lang=en`` (base) + ``&d=<prefix>`` fan-out
    over the same device prefixes ``update_hms_actions.py`` /
    ``backend/app/data/hms_actions.json`` use.
  * ``https://wiki.bambulab.com/en/hms/home`` (scrape) + constructed hmscode paths.

Run from the repo root:  ``.venv\\Scripts\\python.exe scripts/update_hms_errors.py``
"""

from __future__ import annotations

import gzip
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DATA_DIR = _REPO_ROOT / "backend" / "app" / "data"
_TEXT_PATH = _DATA_DIR / "hms_error_text_en.json.gz"
_WIKI_PATH = _DATA_DIR / "hms_wiki_links_en.json.gz"
_PROBE_CACHE_PATH = Path(__file__).resolve().parent / "hms_wiki_probe_cache.json"
_HMS_ACTIONS_PATH = _DATA_DIR / "hms_actions.json"

_QUERY_URL = "https://e.bambulab.com/query.php"
_WIKI_HOME = "https://wiki.bambulab.com/en/hms/home"
_WIKI_BASE = "https://wiki.bambulab.com"

# The MicroSD "not enough space" code the live fleet reports — its deep link MUST
# resolve (mandatory acceptance criterion). Also seeds the probe if all else fails.
_TARGET_CODE = "0500010000030004"

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; FarmManagerHMSSync/1.0)"}
_PROBE_CONCURRENCY = 4
_PROBE_DELAY_S = 0.1  # politeness delay per request, per worker
_WIKI_DEVICES = ("x1", "h2")  # constructed-path device order for uncovered codes


def _device_prefixes() -> list[str]:
    """The 3-letter device prefixes to fan out ``&d=`` over (from hms_actions.json)."""
    try:
        with _HMS_ACTIONS_PATH.open("r", encoding="utf-8") as f:
            keys = list(json.load(f).keys())
    except (OSError, ValueError):
        keys = []
    return [k for k in keys if k and k != "default"]


def _collect_text(data: dict) -> dict[str, str]:
    """Flatten a query.php ``data`` object into ``{ECODE_UPPER: intro}``."""
    out: dict[str, str] = {}
    for value in data.values():
        if isinstance(value, dict):
            for item in value.get("en", []):
                ecode = str(item.get("ecode", "")).upper()
                intro = item.get("intro")
                if ecode and intro:
                    out.setdefault(ecode, intro)
    return out


def fetch_error_text() -> dict[str, str]:
    """Fetch + merge the base catalog and the device-prefix fan-out."""
    merged: dict[str, str] = {}
    base = requests.get(_QUERY_URL, params={"lang": "en"}, timeout=90)
    base.raise_for_status()
    merged.update(_collect_text(base.json().get("data", {})))
    print(f"[text] base lang=en -> {len(merged)} entries")

    for prefix in _device_prefixes():
        try:
            resp = requests.get(_QUERY_URL, params={"lang": "en", "d": prefix}, timeout=90)
            resp.raise_for_status()
            before = len(merged)
            for ecode, intro in _collect_text(resp.json().get("data", {})).items():
                merged.setdefault(ecode, intro)
            print(f"[text] d={prefix} -> +{len(merged) - before} (total {len(merged)})")
        except (requests.RequestException, ValueError) as exc:
            print(f"[text] d={prefix} FAILED: {exc}")
    return merged


def _group_code(code16: str) -> str:
    """``0500010000030004`` -> ``0500_0100_0003_0004`` (wiki path form)."""
    c = code16.upper()
    return "_".join(c[i : i + 4] for i in range(0, 16, 4))


def scrape_wiki_index() -> dict[str, str]:
    """Scrape the HMS home page for ``hmscode`` deep links.

    Returns ``{ECODE16_UPPER: path}``. First device seen for a given code wins.
    """
    links: dict[str, str] = {}
    try:
        resp = requests.get(_WIKI_HOME, headers=_HEADERS, timeout=90)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"[wiki] home scrape FAILED: {exc}")
        return links
    # href="/en/<device>/troubleshooting/hmscode/XXXX_XXXX_XXXX_XXXX"
    for path, grouped in re.findall(
        r"href=[\"']([^\"']*?/hmscode/([0-9A-Fa-f]{4}(?:_[0-9A-Fa-f]{4}){3}))[\"']",
        resp.text,
    ):
        key = grouped.replace("_", "").upper()
        links.setdefault(key, path)
    print(f"[wiki] scraped {len(links)} unique hmscode deep links")
    return links


def _load_probe_cache() -> dict[str, str | None]:
    if _PROBE_CACHE_PATH.exists():
        try:
            with _PROBE_CACHE_PATH.open("r", encoding="utf-8") as f:
                return json.load(f)
        except ValueError:
            return {}
    return {}


def _save_probe_cache(cache: dict[str, str | None]) -> None:
    tmp = _PROBE_CACHE_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, sort_keys=True)
    tmp.replace(_PROBE_CACHE_PATH)


def _probe_one(code16: str) -> tuple[str, str | None]:
    """HEAD-probe ``/en/x1`` then ``/en/h2`` for a code; return (code, path|None)."""
    grouped = _group_code(code16)
    for device in _WIKI_DEVICES:
        path = f"/en/{device}/troubleshooting/hmscode/{grouped}"
        try:
            resp = requests.head(_WIKI_BASE + path, headers=_HEADERS, timeout=30, allow_redirects=True)
            time.sleep(_PROBE_DELAY_S)
            if resp.status_code == 200:
                return code16, path
        except requests.RequestException:
            time.sleep(_PROBE_DELAY_S)
    return code16, None


def build_wiki_links(catalog16: set[str]) -> dict[str, str]:
    """Scrape + probe deep links for the 16-hex catalog codes.

    Probe hits and misses persist to the committed cache so reruns skip them.
    Ships whatever coverage it has if the wiki throttles/blocks.
    """
    scraped = scrape_wiki_index()
    links: dict[str, str] = {c: p for c, p in scraped.items() if c in catalog16}

    cache = _load_probe_cache()
    # Fold cached hits into the link set immediately.
    for code, path in cache.items():
        if path and code in catalog16:
            links.setdefault(code, path)

    to_probe = sorted(catalog16 - set(links) - set(cache))
    # Always ensure the mandatory target gets (re)probed if not already a hit.
    if _TARGET_CODE in catalog16 and _TARGET_CODE not in links and cache.get(_TARGET_CODE) is None:
        if _TARGET_CODE not in to_probe:
            to_probe.insert(0, _TARGET_CODE)
        cache.pop(_TARGET_CODE, None)

    print(f"[wiki] probing {len(to_probe)} uncovered codes (concurrency {_PROBE_CONCURRENCY})")
    done = 0
    try:
        with ThreadPoolExecutor(max_workers=_PROBE_CONCURRENCY) as pool:
            for code, path in pool.map(_probe_one, to_probe):
                cache[code] = path
                if path:
                    links[code] = path
                done += 1
                if done % 200 == 0:
                    _save_probe_cache(cache)
                    print(f"[wiki] probed {done}/{len(to_probe)} (hits so far: {len(links)})")
    finally:
        _save_probe_cache(cache)
    print(f"[wiki] deep-link coverage: {len(links)}/{len(catalog16)} 16-hex codes")
    return links


def _write_gzip_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")
    with gzip.open(path, "wb", compresslevel=9) as f:
        f.write(raw)
    print(f"[write] {path.name}: {len(obj)} entries, {path.stat().st_size} bytes gz")


def main() -> None:
    text = fetch_error_text()
    _write_gzip_json(_TEXT_PATH, text)

    catalog16 = {c for c in text if len(c) == 16}
    wiki = build_wiki_links(catalog16)
    _write_gzip_json(_WIKI_PATH, wiki)

    target_path = wiki.get(_TARGET_CODE)
    print(f"[check] target {_TARGET_CODE} text: {text.get(_TARGET_CODE)!r}")
    print(f"[check] target {_TARGET_CODE} wiki: {target_path!r}")
    if not target_path:
        print("[check] WARNING: mandatory target has no deep link (wiki may be blocking)")


if __name__ == "__main__":
    main()
