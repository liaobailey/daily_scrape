"""
NBA Daily Scraper — ESPN Edition (All Players)
================================================
Saves every player who played in each game, with a starter flag.
Also maintains a season-long player log (player_log.json) to detect:
  - First-time starters (or first 5 games starting this season)
  - Players who played significantly more minutes than usual
  - Notable DNPs for regular starters
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
PLAYER_LOG = DATA_DIR / "player_log.json"

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
    formatted = date_str.replace("-", "")
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
            "home_score": _int(home.get("score", 0)),
            "away_team": away.get("team", {}).get("displayName", ""),
            "away_tricode": away.get("team", {}).get("abbreviation", ""),
            "away_score": _int(away.get("score", 0)),
        })

    log.info(f"Found {len(games)} game(s).")
    return games


# ---------------------------------------------------------------------------
# 2. Get ALL players from ESPN summary API
# ---------------------------------------------------------------------------
def get_players(game_id: str) -> dict:
    """Fetch all players (starters + bench) from ESPN game summary API."""
    log.info(f"  Fetching summary API for {game_id}...")
    data = fetch_json(ESPN_SUMMARY, params={"event": game_id})
    if not data:
        return {"home": [], "away": []}

    result = {"home": [], "away": []}
    boxscore = data.get("boxscore", {})
    team_order = []  # track order: first team listed = away

    for player_group in boxscore.get("players", []):
        team_info = player_group.get("team", {})
        tricode = team_info.get("abbreviation", "")
        home_away = team_info.get("homeAway", "")
        team_order.append(home_away or ("away" if len(team_order) == 0 else "home"))

        key = home_away if home_away in ("home", "away") else team_order[-1]

        for stat_group in player_group.get("statistics", []):
            athletes = stat_group.get("athletes", [])
            labels = stat_group.get("labels", [])

            for athlete in athletes:
                player = athlete.get("athlete", {})
                stats_raw = athlete.get("stats", [])

                stat_map = {}
                for j, label in enumerate(labels):
                    if j < len(stats_raw):
                        stat_map[label] = stats_raw[j]

                # Parse minutes: ESPN format is "32:15" or "DNP" or "--"
                min_str = stat_map.get("MIN", "0")
                minutes = _parse_minutes(min_str)

                did_not_play = min_str in ("DNP", "--", "") or minutes == 0

                entry = {
                    "name": player.get("displayName", ""),
                    "id": player.get("id", ""),
                    "position": player.get("position", {}).get("abbreviation", ""),
                    "starter": athlete.get("starter", False),
                    "dnp": did_not_play,
                    "min": minutes,
                    "pts": _int(stat_map.get("PTS", "0")),
                    "reb": _int(stat_map.get("REB", "0")),
                    "ast": _int(stat_map.get("AST", "0")),
                    "stl": _int(stat_map.get("STL", "0")),
                    "blk": _int(stat_map.get("BLK", "0")),
                    "to": _int(stat_map.get("TO", "0")),
                    "fgm": _int(stat_map.get("FGM", stat_map.get("FG", "0").split("-")[0] if "-" in stat_map.get("FG", "") else "0")),
                    "fga": _int(stat_map.get("FGA", stat_map.get("FG", "0").split("-")[1] if "-" in stat_map.get("FG", "") else "0")),
                    "team": tricode,
                }

                result[key].append(entry)

    return result


# ---------------------------------------------------------------------------
# 3. Get written recap
# ---------------------------------------------------------------------------
def get_recap(game_id: str) -> str:
    log.info(f"  Fetching recap for {game_id}...")
    for url in [ESPN_RECAP.format(game_id=game_id), ESPN_RECAP_ALT.format(game_id=game_id)]:
        html = fetch_html(url)
        if not html:
            continue

        soup = BeautifulSoup(html, "html.parser")
        for selector in [
            {"class_": re.compile(r"Story__Body|article-body|gameRecap", re.I)},
            {"class_": re.compile(r"story", re.I)},
        ]:
            container = soup.find("div", **selector)
            if container:
                paragraphs = container.find_all("p")
                if paragraphs:
                    text = " ".join(p.get_text(separator=" ", strip=True) for p in paragraphs)
                    text = re.sub(r'\s+', ' ', text).strip()
                    if len(text) > 50:
                        return text

        article = soup.find("article")
        if article:
            text = article.get_text(separator=" ", strip=True)
            if len(text) > 50:
                return text

    return ""


# ---------------------------------------------------------------------------
# 4. Player log — track season-long per-player data
# ---------------------------------------------------------------------------
def load_player_log() -> dict:
    """Load the season-long player log. Structure:
    { "player_id": { "name": str, "team": str, "games": int,
                      "starts": int, "total_min": float, "dates_started": [...] } }
    """
    if PLAYER_LOG.exists():
        with open(PLAYER_LOG) as f:
            return json.load(f)
    return {}


def save_player_log(plog: dict):
    with open(PLAYER_LOG, "w") as f:
        json.dump(plog, f, indent=2)


def update_player_log(plog: dict, players: list[dict], date_str: str):
    """Update the player log with today's game data."""
    for p in players:
        pid = p.get("id", "")
        if not pid or p.get("dnp"):
            continue

        if pid not in plog:
            plog[pid] = {
                "name": p["name"],
                "team": p.get("team", ""),
                "games": 0,
                "starts": 0,
                "total_min": 0,
                "dates_started": [],
            }

        entry = plog[pid]
        entry["name"] = p["name"]
        entry["team"] = p.get("team", entry["team"])
        entry["games"] += 1
        entry["total_min"] += p.get("min", 0)

        if p.get("starter"):
            entry["starts"] += 1
            entry["dates_started"].append(date_str)


def generate_blurbs(plog: dict, all_players: list[dict], date_str: str) -> list[str]:
    """Generate noteworthy blurbs based on player log context."""
    blurbs = []

    for p in all_players:
        pid = p.get("id", "")
        if not pid or p.get("dnp"):
            continue

        entry = plog.get(pid, {})
        games_before = entry.get("games", 1) - 1  # subtract current game
        avg_min = entry.get("total_min", 0) / max(entry.get("games", 1), 1)
        starts = entry.get("starts", 0)
        minutes = p.get("min", 0)
        name = p["name"]
        team = p.get("team", "")

        # First career start this season (or first 5)
        if p.get("starter") and starts <= 5 and games_before >= 5:
            if starts == 1:
                blurbs.append(f"🆕 {name} ({team}) made his first start of the season. {minutes:.0f} min, {p['pts']} pts.")
            elif starts <= 5:
                blurbs.append(f"📋 {name} ({team}) made just his {_ordinal(starts)} start this season. {p['pts']} pts, {p['reb']} reb.")

        # Played way more than usual (50%+ more than average, min 10 min increase)
        elif games_before >= 10 and minutes > 0:
            prev_avg = (entry.get("total_min", 0) - minutes) / max(games_before, 1)
            if prev_avg > 0 and minutes >= prev_avg * 1.5 and (minutes - prev_avg) >= 10:
                blurbs.append(
                    f"⬆️ {name} ({team}) played {minutes:.0f} min (season avg: {prev_avg:.0f}). "
                    f"{p['pts']} pts, {p['reb']} reb, {p['ast']} ast."
                )

    return blurbs


# ---------------------------------------------------------------------------
# Summary condensation
# ---------------------------------------------------------------------------
def condense(text: str) -> str:
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
            score += 4
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


def _parse_minutes(val: str) -> float:
    """Parse ESPN minutes format like '32:15' or '32' into float minutes."""
    if not val or val in ("DNP", "--", ""):
        return 0
    try:
        if ":" in val:
            parts = val.split(":")
            return int(parts[0]) + int(parts[1]) / 60
        return float(val)
    except (ValueError, TypeError):
        return 0


def _ordinal(n: int) -> str:
    if 11 <= n <= 13:
        return f"{n}th"
    return f"{n}{['th','st','nd','rd'][n%10] if n%10 < 4 else 'th'}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    target = sys.argv[1] if len(sys.argv) > 1 else None
    date_label = target or datetime.now(ET).strftime("%Y-%m-%d")

    log.info(f"=== NBA Daily Scraper (ESPN) — {date_label} ===")

    # Load player log
    plog = load_player_log()

    # 1. Get games
    games = get_games(date_label)

    # 2. Enrich each game
    results = []
    for g in games:
        gid = g["espn_id"]
        time.sleep(DELAY)

        # All players from summary API
        players_data = get_players(gid)
        time.sleep(DELAY)

        # Written recap
        recap_text = get_recap(gid)
        summary = condense(recap_text) if recap_text else ""

        # Fallback summary
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

        # Update player log with all players who played
        all_game_players = players_data.get("home", []) + players_data.get("away", [])
        update_player_log(plog, all_game_players, date_label)

        # Generate blurbs about unusual performances
        blurbs = generate_blurbs(plog, all_game_players, date_label)

        results.append({
            "game_id": gid,
            "status": g["status"],
            "home": {
                "name": g["home_team"],
                "tricode": g["home_tricode"],
                "score": g["home_score"],
                "players": players_data.get("home", []),
            },
            "away": {
                "name": g["away_team"],
                "tricode": g["away_tricode"],
                "score": g["away_score"],
                "players": players_data.get("away", []),
            },
            "summary": summary,
            "blurbs": blurbs,
        })

    # Write game data
    out = {"date": date_label, "games": results}
    out_file = DATA_DIR / f"{date_label}.json"
    with open(out_file, "w") as f:
        json.dump(out, f, indent=2)
    log.info(f"Wrote {len(results)} game(s) to {out_file}")

    # Save player log
    save_player_log(plog)
    log.info(f"Player log: {len(plog)} players tracked.")

    # Update index
    dates = sorted(
        [p.stem for p in DATA_DIR.glob("*.json") if p.stem != "index" and p.stem != "player_log"],
        reverse=True,
    )
    with open(DATA_DIR / "index.json", "w") as f:
        json.dump({"dates": dates}, f, indent=2)
    log.info(f"Index: {len(dates)} date(s).")


if __name__ == "__main__":
    main()
