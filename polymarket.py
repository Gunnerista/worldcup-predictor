"""
polymarket.py
=============

Polymarket Gamma API client for FIFA World Cup 2026 tournament-level markets.

NOTE: Polymarket does NOT currently list per-match W/D/L markets for the World Cup
(verified 2026-06-15). The functions below cover what IS available: knockout-round
qualification, group winner, tournament awards, and one-off tournament props.

All public functions:
  - Return None / empty when the underlying market does not exist
  - Never raise on network or parse errors (they swallow and return None / [])
  - Treat `outcomePrices` as a JSON-encoded string (per Gamma API behaviour)

Run directly to see a demo:
    python polymarket.py
"""

from __future__ import annotations

import json
import sys
from typing import Optional

import requests

BASE = "https://gamma-api.polymarket.com"
TIMEOUT = 15
PAGE_LIMIT = 100
MAX_EVENTS = 1000  # safety cap on pagination


# ---------------------------------------------------------------------------
# Parsing helpers (Gamma API quirks)
# ---------------------------------------------------------------------------

def _parse_json_list(raw) -> Optional[list]:
    """Gamma sends list fields as JSON-encoded strings. Return list or None."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
        return parsed if isinstance(parsed, list) else None
    return None


def _parse_prices(raw) -> Optional[list[float]]:
    items = _parse_json_list(raw)
    if items is None:
        return None
    try:
        return [float(x) for x in items]
    except (TypeError, ValueError):
        return None


def _parse_outcomes(raw) -> Optional[list[str]]:
    items = _parse_json_list(raw)
    if items is None:
        return None
    return [str(x) for x in items]


def _yes_probability(market: dict) -> Optional[float]:
    """For a binary Yes/No market, return the Yes-side price (= implied probability)."""
    outcomes = _parse_outcomes(market.get("outcomes"))
    prices = _parse_prices(market.get("outcomePrices"))
    if not outcomes or not prices or len(outcomes) != len(prices):
        return None
    for o, p in zip(outcomes, prices):
        if o.strip().lower() == "yes":
            return p
    return None


def _is_tradeable(market: dict) -> bool:
    return bool(market.get("active")) and not market.get("closed")


def _team_match(item_title: str, query: str) -> bool:
    if not item_title or not query:
        return False
    return query.strip().lower() in item_title.strip().lower()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_worldcup_markets() -> list[dict]:
    """Fetch all active (non-closed) World Cup events, with nested `markets`.

    Returns an empty list on network failure.
    """
    all_events: list[dict] = []
    offset = 0
    while offset < MAX_EVENTS:
        try:
            r = requests.get(
                f"{BASE}/events",
                params={
                    "tag_slug": "world-cup",
                    "closed": "false",
                    "limit": PAGE_LIMIT,
                    "offset": offset,
                },
                timeout=TIMEOUT,
            )
            r.raise_for_status()
            page = r.json()
        except (requests.RequestException, ValueError):
            break
        if not isinstance(page, list) or not page:
            break
        all_events.extend(page)
        if len(page) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
    return all_events


_ROUND_TO_TITLE = {
    "16": "Round of 16",
    "round of 16": "Round of 16",
    "r16": "Round of 16",
    "8": "Quarterfinals",
    "quarter": "Quarterfinals",
    "quarterfinals": "Quarterfinals",
    "qf": "Quarterfinals",
    "4": "Semifinals",
    "semi": "Semifinals",
    "semifinals": "Semifinals",
    "sf": "Semifinals",
}


def get_qualification_odds(team_name: str, round: str = "16") -> Optional[float]:
    """Probability that `team_name` reaches a given knockout round.

    `round` accepts "16", "8", "4" (or "quarter"/"semi"). Returns None if the
    market is missing, the team is not listed, or the market is closed.
    """
    target = _ROUND_TO_TITLE.get(str(round).strip().lower())
    if not target:
        return None

    needle = f"nation to reach {target}".lower()
    events = get_worldcup_markets()
    for ev in events:
        if needle not in (ev.get("title") or "").lower():
            continue
        for m in ev.get("markets", []):
            if not _is_tradeable(m):
                continue
            if _team_match(m.get("groupItemTitle") or "", team_name):
                return _yes_probability(m)
    return None


def get_group_winner_odds(team_name: str) -> Optional[float]:
    """Probability that `team_name` wins its World Cup group."""
    events = get_worldcup_markets()
    for ev in events:
        title = (ev.get("title") or "").lower()
        # match "World Cup Group X Winner" pattern, exclude group-stage / last / second
        if "group" not in title or "winner" not in title:
            continue
        if "stage" in title or "last" in title or "second" in title:
            continue
        for m in ev.get("markets", []):
            if not _is_tradeable(m):
                continue
            if _team_match(m.get("groupItemTitle") or "", team_name):
                return _yes_probability(m)
    return None


_AWARD_TO_TITLE = {
    "golden_boot": "Golden Boot",
    "silver_boot": "Silver Boot",
    "bronze_boot": "Bronze Boot",
    "golden_ball": "Golden Ball",
    "silver_ball": "Silver Ball",
    "bronze_ball": "Bronze Ball",
    "golden_glove": "Golden Glove",
}


def get_award_odds(award: str = "golden_boot") -> Optional[dict[str, float]]:
    """Probability distribution over players for a tournament award.

    Returns {player_name: yes_probability}, ordered descending by probability.
    Returns None if the market for that award is not found.
    """
    needle = _AWARD_TO_TITLE.get(str(award).strip().lower())
    if not needle:
        return None
    needle_lower = f"{needle} winner".lower()

    events = get_worldcup_markets()
    target_event = None
    for ev in events:
        if needle_lower in (ev.get("title") or "").lower():
            target_event = ev
            break
    if target_event is None:
        return None

    out: dict[str, float] = {}
    for m in target_event.get("markets", []):
        if not _is_tradeable(m):
            continue
        name = m.get("groupItemTitle") or ""
        if not name:
            continue
        p = _yes_probability(m)
        if p is None:
            continue
        out[name] = p

    if not out:
        return None
    return dict(sorted(out.items(), key=lambda kv: kv[1], reverse=True))


# Event-title substrings that mark a multi-market category (NOT a single-question prop)
_PROP_EXCLUDE = (
    "winner",                # group winner, award winner
    "second place",
    "last place",
    "nation to reach",       # knockout qualification
    "furthest advancing",
    "worst-placed",
    "highest-scoring team",
    "h2h",                   # head-to-head player markets
    "no. of matches",        # series of markets
    "group of champion",
    "which team will replace",
    "which continent",
)


def get_tournament_props() -> list[dict]:
    """One-off tournament-level Yes/No props.

    Examples: "Any team to score 10+ in group stage?", "Trump to attend final?",
    "Messi to score a free kick?".

    Each entry: {"question", "yes_probability", "event_title", "slug"}.
    Returns [] if nothing matches.
    """
    events = get_worldcup_markets()
    props: list[dict] = []
    for ev in events:
        title_lower = (ev.get("title") or "").lower()
        if any(b in title_lower for b in _PROP_EXCLUDE):
            continue
        markets = ev.get("markets", [])
        if len(markets) != 1:
            continue
        m = markets[0]
        if not _is_tradeable(m):
            continue
        outcomes = _parse_outcomes(m.get("outcomes"))
        if not outcomes or sorted(o.lower() for o in outcomes) != ["no", "yes"]:
            continue
        yes_p = _yes_probability(m)
        if yes_p is None:
            continue
        props.append({
            "question": m.get("question") or ev.get("title"),
            "yes_probability": yes_p,
            "event_title": ev.get("title"),
            "slug": m.get("slug") or ev.get("slug"),
        })
    return props


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

def _fmt_pct(p: Optional[float]) -> str:
    return f"{p:.1%}" if p is not None else "n/a"


def _demo() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    print("=" * 72)
    print("polymarket.py -- demo")
    print("=" * 72)

    print()
    print("[1] South Korea  --  Reach Round of 16")
    print(f"    Yes probability: {_fmt_pct(get_qualification_odds('South Korea', round='16'))}")

    print()
    print("[2] Spain  --  Win their group")
    print(f"    Yes probability: {_fmt_pct(get_group_winner_odds('Spain'))}")

    print()
    print("[3] Golden Boot  --  top 5")
    odds = get_award_odds("golden_boot")
    if odds:
        for i, (player, prob) in enumerate(list(odds.items())[:5], 1):
            print(f"    {i}. {player:30s} {_fmt_pct(prob)}")
    else:
        print("    not found on Polymarket right now")

    print()
    print("[4] Tournament props  --  sample 3")
    props = get_tournament_props()
    if props:
        for i, p in enumerate(props[:3], 1):
            print(f"    {i}. {p['question']}")
            print(f"       Yes {_fmt_pct(p['yes_probability'])}  (event: {p['event_title']})")
    else:
        print("    no tournament props found")


if __name__ == "__main__":
    _demo()
