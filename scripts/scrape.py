"""
NBA Daily Scraper — ESPN Edition
==================================
Uses ESPN's free public API + recap pages. No API key, no headless browser.

Endpoints:
  - Scoreboard:  site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=YYYYMMDD
  - Game summary: site.web.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={id}
  - Recap HTML:   espn.com/nba/recap?gameId={id}  (server-rendered, scrapeable)
"""

import json
import logging
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
ET = timezone(timedelta(hours=-5))
DOCS = Path(__file__).resolve().parent.parent / "docs"
DATA_DIR = DOCS / "data"

ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
ESPN_SUMMARY = "https://site.web.api.espn.com/apis/site/v2/sports/basketball/nba/summary"
ESPN_RECAP = "https://www.espn.com/nba/recap?gameId={game_id}"
ESPN_RECAP_ALT = "https://www.espn.com/nba/recap/_/gameId/{game_id}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

DELAY = 0.5

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def fetch_json(url, params=None, retries=3):
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning(f"  Attempt {i+1}: {e}")
            if i < retries - 1:
                time.sleep(2 ** i)
    return None


def fetch_html(url, retries=3):
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()
            return r.text
        except Exception as e:
            log.warning(f"  Attempt {i+1}: {e}")
            if i < retries - 1:
                time.sleep(2 ** i)
    return None


# ---------------------------------------------------------------------------
# 1. Get games for a date
# ---------------------------------------------------------------------------
def get_games(date_str: str) -> list[dict]:
    """Fetch all games for a date from ESPN scoreboard API."""
    formatted = date_str.replace("-", "")  # YYYYMMDD
    log.info(f"Fetching ESPN scoreboard for {date_str}...")
    data = fetch_json(ESPN_SCOREBOARD, params={"dates": formatted})
    if not data:
        return []

    games = []
    for event in data.get("events", []):
        competition = event.get("competitions", [{}])[0]
        competitors = competition.get("competitors", [])

        home = next((c for c in competitors if c.get("homeAway") == "home"), {})
        away = next((c for c in competitors if c.get("homeAway") == "away"), {})

        games.append({
            "espn_id": event.get("id", ""),
            "status": event.get("status", {}).get("type", {}).get("description", ""),
            "home_team": home.get("team", {}).get("displayName", ""),
            "home_tricode": home.get("team", {}).get("abbreviation", ""),
            "home_score": int(home.get("score", 0)),
            "away_team": away.get("team", {}).get("displayName", ""),
            "away_tricode": away.get("team", {}).get("abbreviation", ""),
            "away_score": int(away.get("score", 0)),
        })

    log.info(f"Found {len(games)} game(s).")
    return games


# ---------------------------------------------------------------------------
# 2. Get box score (starters) from ESPN summary API
# ---------------------------------------------------------------------------
def get_starters(game_id: str) -> dict:
    """Fetch starters from ESPN game summary API."""
    log.info(f"  Fetching summary API for {game_id}...")
    data = fetch_json(ESPN_SUMMARY, params={"event": game_id})
    if not data:
        return {"home": [], "away": []}

    result = {"home": [], "away": []}
    boxscore = data.get("boxscore", {})

    for player_group in boxscore.get("players", []):
        team_info = player_group.get("team", {})
        home_away = team_info.get("homeAway", "")  # not always present
        tricode = team_info.get("abbreviation", "")

        # Determine home/away from the team data
        key = home_away if home_away in ("home", "away") else None

        for stat_group in player_group.get("statistics", []):
            athletes = stat_group.get("athletes", [])
            labels = stat_group.get("labels", [])

            for athlete in athletes:
                is_starter = athlete.get("starter", False)
                if not is_starter:
                    continue

                player = athlete.get("athlete", {})
                stats = athlete.get("stats", [])

                # Map labels to stats
                stat_map = {}
                for j, label in enumerate(labels):
                    if j < len(stats):
                        stat_map[label] = stats[j]

                entry = {
                    "name": player.get("displayName", ""),
                    "position": player.get("position", {}).get("abbreviation", ""),
                    "pts": _int(stat_map.get("PTS", "0")),
                    "reb": _int(stat_map.get("REB", "0")),
                    "ast": _int(stat_map.get("AST", "0")),
                    "stl": _int(stat_map.get("STL", "0")),
                    "blk": _int(stat_map.get("BLK", "0")),
                }

                if key:
                    result[key].append(entry)
                else:
                    # Fallback: first team = away, second = home (ESPN convention)
                    result.setdefault("_teams", []).append((tricode, entry))

    # Handle fallback ordering if homeAway wasn't available
    if "_teams" in result:
        teams_seen = []
        for tri, entry in result["_teams"]:
            if tri not in teams_seen:
                teams_seen.append(tri)
            idx = teams_seen.index(tri)
            key = "away" if idx == 0 else "home"
            result[key].append(entry)
        del result["_teams"]

    return result


# ---------------------------------------------------------------------------
# 3. Get written recap from ESPN recap page
# ---------------------------------------------------------------------------
def get_recap(game_id: str) -> str:
    """Scrape the written recap from ESPN's recap page."""
    log.info(f"  Fetching recap page for {game_id}...")

    # Try both URL formats
    for url in [ESPN_RECAP.format(game_id=game_id), ESPN_RECAP_ALT.format(game_id=game_id)]:
        html = fetch_html(url)
        if not html:
            continue

        soup = BeautifulSoup(html, "html.parser")

        # ESPN recap text is usually in <div class="Story__Body"> or article tags
        for selector in [
            {"class_": re.compile(r"Story__Body|article-body|gameRecap", re.I)},
            {"class_": re.compile(r"story", re.I)},
        ]:
            container = soup.find("div", **selector)
            if container:
                # Get all paragraph text
                paragraphs = container.find_all("p")
                if paragraphs:
                    text = " ".join(p.get_text(strip=True) for p in paragraphs)
                    if len(text) > 50:
                        return text

        # Fallback: find article tag
        article = soup.find("article")
        if article:
            text = article.get_text(separator=" ", strip=True)
            if len(text) > 50:
                return text

        # Last resort: find the longest text block
        for p in soup.find_all("p"):
            text = p.get_text(strip=True)
            if len(text) > 150:
                return text

    return ""


# ---------------------------------------------------------------------------
# Summary condensation
# ---------------------------------------------------------------------------
def condense(text: str) -> str:
    """Condense recap to 1-2 key sentences."""
    if not text or len(text) < 30:
        return text

    text = re.sub(r'\s+', ' ', text).strip()
    sentences = re.split(r'(?<=[.!?])\s+', text)
    if not sentences:
        return text[:250]

    keywords = [
        'triple-double', 'double-double', 'career-high', 'season-high',
        'scored', 'led', 'points', 'clutch', 'overtime', 'debut',
        'injury', 'returned', 'traded', 'record', 'streak', 'historic',
        'milestone', 'ejected', 'first time', 'consecutive',
    ]

    scored = []
    for i, s in enumerate(sentences):
        lower = s.lower()
        score = sum(2 for kw in keywords if kw in lower)
        if i == 0:
            score += 4  # first sentence usually has the key result
        scored.append((score, i, s))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:2]
    top.sort(key=lambda x: x[1])

    summary = ' '.join(s for _, _, s in top)
    if len(summary) > 350:
        summary = summary[:347] + '...'
    return summary


def _int(val):
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    target = sys.argv[1] if len(sys.argv) > 1 else None
    date_label = target or datetime.now(ET).strftime("%Y-%m-%d")

    log.info(f"=== NBA Daily Scraper (ESPN) — {date_label} ===")

    # 1. Get games
    games = get_games(date_label)

    # 2. Enrich each game with starters + recap
    results = []
    for g in games:
        gid = g["espn_id"]
        time.sleep(DELAY)

        # Starters from summary API
        starters = get_starters(gid)
        time.sleep(DELAY)

        # Written recap
        recap_text = get_recap(gid)
        summary = condense(recap_text) if recap_text else ""

        # Fallback summary from score
        if not summary:
            hs, as_ = g["home_score"], g["away_score"]
            if hs > 0 or as_ > 0:
                winner = g["home_team"] if hs > as_ else g["away_team"]
                loser = g["away_team"] if hs > as_ else g["home_team"]
                hi, lo = max(hs, as_), min(hs, as_)
                margin = hi - lo
                if margin >= 20:
                    summary = f"{winner} blowout, {hi}-{lo}."
                elif margin <= 5:
                    summary = f"{winner} edges {loser}, {hi}-{lo}."
                else:
                    summary = f"{winner} def. {loser}, {hi}-{lo}."

        results.append({
            "game_id": gid,
            "status": g["status"],
            "home": {
                "name": g["home_team"],
                "tricode": g["home_tricode"],
                "score": g["home_score"],
                "starters": starters.get("home", []),
            },
            "away": {
                "name": g["away_team"],
                "tricode": g["away_tricode"],
                "score": g["away_score"],
                "starters": starters.get("away", []),
            },
            "summary": summary,
        })

    # Write output
    out = {"date": date_label, "games": results}
    out_file = DATA_DIR / f"{date_label}.json"
    with open(out_file, "w") as f:
        json.dump(out, f, indent=2)
    log.info(f"Wrote {len(results)} game(s) to {out_file}")

    # Update index
    dates = sorted(
        [p.stem for p in DATA_DIR.glob("*.json") if p.stem != "index"],
        reverse=True,
    )
    with open(DATA_DIR / "index.json", "w") as f:
        json.dump({"dates": dates}, f, indent=2)
    log.info(f"Index: {len(dates)} date(s).")


if __name__ == "__main__":
    main()
