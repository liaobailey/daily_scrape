"""
NBA Backfill Scraper
====================
Scrapes all games for a range of dates.
Usage: python scripts/backfill.py 2025-10-22 2026-02-13
"""

import sys
import time
from datetime import datetime, timedelta

# Import everything from the main scraper
from scrape import (
    get_games, get_starters, get_recap, condense,
    DATA_DIR, log, DELAY,
)
import json


def scrape_date(date_str: str) -> dict:
    """Scrape all games for a single date."""
    games = get_games(date_str)
    results = []

    for g in games:
        gid = g["espn_id"]
        time.sleep(DELAY)

        starters = get_starters(gid)
        time.sleep(DELAY)

        recap_text = get_recap(gid)
        summary = condense(recap_text) if recap_text else ""

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

    return {"date": date_str, "games": results}


def main():
    if len(sys.argv) < 3:
        print("Usage: python scripts/backfill.py START_DATE END_DATE")
        print("       python scripts/backfill.py 2025-10-22 2026-02-13")
        sys.exit(1)

    start = datetime.strptime(sys.argv[1], "%Y-%m-%d")
    end = datetime.strptime(sys.argv[2], "%Y-%m-%d")

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    current = start
    total_days = (end - start).days + 1
    day_num = 0

    while current <= end:
        day_num += 1
        date_str = current.strftime("%Y-%m-%d")
        out_file = DATA_DIR / f"{date_str}.json"

        # Skip if already scraped
        if out_file.exists():
            log.info(f"[{day_num}/{total_days}] {date_str} — already exists, skipping.")
            current += timedelta(days=1)
            continue

        log.info(f"[{day_num}/{total_days}] Scraping {date_str}...")

        try:
            data = scrape_date(date_str)
            with open(out_file, "w") as f:
                json.dump(data, f, indent=2)
            log.info(f"  Wrote {len(data['games'])} game(s).")
        except Exception as e:
            log.error(f"  Failed: {e}")

        # Rate limit: pause between days
        time.sleep(1)
        current += timedelta(days=1)

    # Update index
    dates = sorted(
        [p.stem for p in DATA_DIR.glob("*.json") if p.stem != "index"],
        reverse=True,
    )
    with open(DATA_DIR / "index.json", "w") as f:
        json.dump({"dates": dates}, f, indent=2)
    log.info(f"Done. Index updated: {len(dates)} date(s).")


if __name__ == "__main__":
    main()
