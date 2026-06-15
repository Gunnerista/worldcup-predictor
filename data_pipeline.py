"""
data_pipeline.py — API connection smoke test.

Run directly: `python data_pipeline.py`

Tests:
  1. BALLDONTLIE  GET /fifa/worldcup/v1/teams
  2. BALLDONTLIE  GET /fifa/worldcup/v1/matches?seasons[]=2026
  3. Polymarket   GET /markets?tag=soccer

Prints PASS/FAIL + up to 3 sample items per endpoint. No data persisted yet.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import requests
from dotenv import load_dotenv

BALLDONTLIE_BASE = "https://api.balldontlie.io/fifa/worldcup/v1"
POLYMARKET_BASE = "https://gamma-api.polymarket.com"
TIMEOUT = 15  # seconds


def _print_header(n: int, title: str) -> None:
    print()
    print("=" * 72)
    print(f"[{n}] {title}")
    print("=" * 72)


def _short(value: Any, maxlen: int = 200) -> str:
    s = json.dumps(value, ensure_ascii=False, default=str)
    return s if len(s) <= maxlen else s[:maxlen] + " ...(truncated)"


def _extract_list(payload: Any) -> list[Any]:
    """BALLDONTLIE returns {'data': [...]}. Polymarket returns a list directly."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return payload["data"]
    return []


def _report(resp: requests.Response, label: str) -> bool:
    ok = resp.ok
    status_word = "PASS" if ok else "FAIL"
    print(f"  -> {status_word}  HTTP {resp.status_code}  ({label})")
    try:
        payload = resp.json()
    except ValueError:
        print(f"  -> body (not JSON, first 300 chars): {resp.text[:300]}")
        return False

    items = _extract_list(payload)
    print(f"  -> items returned: {len(items)}")

    if not ok:
        print(f"  -> error body: {_short(payload, 400)}")
        return False

    for i, item in enumerate(items[:3], start=1):
        print(f"  -> sample {i}: {_short(item, 300)}")
    return True


def test_balldontlie_teams(api_key: str) -> bool:
    _print_header(1, "BALLDONTLIE  /teams")
    url = f"{BALLDONTLIE_BASE}/teams"
    headers = {"Authorization": api_key}
    try:
        resp = requests.get(url, headers=headers, timeout=TIMEOUT)
    except requests.RequestException as e:
        print(f"  -> FAIL  network error: {e}")
        return False
    return _report(resp, "teams")


def test_balldontlie_matches_2026(api_key: str) -> bool:
    _print_header(2, "BALLDONTLIE  /matches?seasons[]=2026")
    url = f"{BALLDONTLIE_BASE}/matches"
    headers = {"Authorization": api_key}
    params = {"seasons[]": "2026"}
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=TIMEOUT)
    except requests.RequestException as e:
        print(f"  -> FAIL  network error: {e}")
        return False
    return _report(resp, "matches 2026")


def test_polymarket_soccer() -> bool:
    _print_header(3, "Polymarket  /markets?tag=soccer")
    url = f"{POLYMARKET_BASE}/markets"
    params = {"tag": "soccer"}
    try:
        resp = requests.get(url, params=params, timeout=TIMEOUT)
    except requests.RequestException as e:
        print(f"  -> FAIL  network error: {e}")
        return False
    return _report(resp, "polymarket soccer")


def main() -> int:
    load_dotenv()
    api_key = os.getenv("BALLDONTLIE_API_KEY", "").strip()

    if not api_key or api_key == "your_key_here":
        print("FATAL: BALLDONTLIE_API_KEY missing or placeholder in .env")
        return 2

    results = [
        ("BALLDONTLIE teams", test_balldontlie_teams(api_key)),
        ("BALLDONTLIE matches 2026", test_balldontlie_matches_2026(api_key)),
        ("Polymarket soccer", test_polymarket_soccer()),
    ]

    print()
    print("=" * 72)
    print("SUMMARY")
    print("=" * 72)
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")

    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    sys.exit(main())
