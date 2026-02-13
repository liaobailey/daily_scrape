"""
NBA Daily Scraper — Playwright Edition
=======================================
Scrapes NBA.com game pages using a headless browser to get:
  1. Final score
  2. Starters (identified by position badge in box score)
  3. Written game summary from the Summary tab (condensed)

Uses Playwright to render the JavaScript-heavy NBA.com React app.
Designed to run in GitHub Actions on a daily cron schedule.
"""

import json
import logging
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ET = timezone(timedelta(hours=-5))
DOCS = Path(__file__).resolve().parent.parent / "docs"
DATA_DIR = DOCS / "data"
SCOREBOARD_URL = "https://www.nba.com/games?date={date}"
GAME_BASE = "https://www.nba.com/game/{slug}"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Scrape scoreboard: get list of game slugs for a given date
# ---------------------------------------------------------------------------
def get_game_slugs(page, date_str: str) -> list[dict]:
    """
    Navigate to nba.com/games?date=YYYY-MM-DD and extract game slugs.
    """
    url = SCOREBOARD_URL.format(date=date_str)
    log.info(f"Loading scoreboard: {url}")

    page.goto(url, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(3000)

    # Extract game links — pattern /game/{away}-vs-{home}-{gameId}
    games = page.evaluate("""
        () => {
            const links = document.querySelectorAll('a[href*="/game/"]');
            const slugs = new Set();
            const results = [];
            links.forEach(link => {
                const href = link.getAttribute('href');
                const match = href.match(/\\/game\\/([a-z]+-vs-[a-z]+-\\d+)/);
                if (match && !slugs.has(match[1])) {
                    slugs.add(match[1]);
                    results.push({ slug: match[1] });
                }
            });
            return results;
        }
    """)

    log.info(f"Found {len(games)} game slug(s).")
    return games


# ---------------------------------------------------------------------------
# Scrape a single game page
# ---------------------------------------------------------------------------
def scrape_game(page, slug: str) -> dict | None:
    """Scrape a single game page for score, starters, and summary text."""
    game_url = GAME_BASE.format(slug=slug)
    log.info(f"Scraping: {game_url}")

    result = {
        "game_id": slug,
        "status": "",
        "home": {"name": "", "tricode": "", "score": 0, "starters": []},
        "away": {"name": "", "tricode": "", "score": 0, "starters": []},
        "summary": "",
        "nba_summary": "",
    }

    # --- Load game page (Summary tab is default) ---
    try:
        page.goto(game_url, wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(4000)
    except Exception as e:
        log.error(f"Failed to load {game_url}: {e}")
        return None

    # --- Extract score, teams, starters, and summary from __NEXT_DATA__ ---
    try:
        extracted = page.evaluate("""
            () => {
                const nd = document.getElementById('__NEXT_DATA__');
                if (!nd) return null;
                try {
                    const json = JSON.parse(nd.textContent);
                    return json?.props?.pageProps || null;
                } catch(e) { return null; }
            }
        """)

        if extracted and extracted.get("game"):
            g = extracted["game"]
            ht = g.get("homeTeam", {})
            at = g.get("awayTeam", {})

            result["status"] = g.get("gameStatusText", "")
            result["home"]["name"] = f"{ht.get('teamCity','')} {ht.get('teamName','')}".strip()
            result["home"]["tricode"] = ht.get("teamTricode", "")
            result["home"]["score"] = ht.get("score", 0)
            result["away"]["name"] = f"{at.get('teamCity','')} {at.get('teamName','')}".strip()
            result["away"]["tricode"] = at.get("teamTricode", "")
            result["away"]["score"] = at.get("score", 0)

            # Starters
            for team_data, key in [(ht, "home"), (at, "away")]:
                for p in team_data.get("players", []):
                    if str(p.get("starter")) == "1":
                        s = p.get("statistics", {})
                        result[key]["starters"].append({
                            "name": f"{p.get('firstName','')} {p.get('familyName','')}".strip(),
                            "position": p.get("position", ""),
                            "pts": s.get("points", 0),
                            "reb": s.get("reboundsTotal", 0),
                            "ast": s.get("assists", 0),
                            "stl": s.get("steals", 0),
                            "blk": s.get("blocks", 0),
                        })
    except Exception as e:
        log.warning(f"__NEXT_DATA__ extraction failed: {e}")

    # --- Extract written summary text from the page ---
    try:
        summary_text = page.evaluate("""
            () => {
                // Try structured data first
                const nd = document.getElementById('__NEXT_DATA__');
                if (nd) {
                    try {
                        const json = JSON.parse(nd.textContent);
                        const pp = json?.props?.pageProps;
                        // Check for article/recap content in pageProps
                        const story = pp?.story || pp?.article || pp?.recap || {};
                        if (story.content) return story.content;
                        if (story.body) return story.body;

                        // Check for summary in game data
                        const gameSummary = pp?.game?.summary;
                        if (gameSummary) return gameSummary;
                    } catch(e) {}
                }

                // Fallback: scrape visible summary text from the page
                const selectors = [
                    'article', '[class*="recap" i]', '[class*="summary" i] p',
                    '[class*="article" i]', '[data-testid*="summary" i]',
                    '[class*="GameSummary"]', '[class*="Summary_article"]',
                ];
                for (const sel of selectors) {
                    try {
                        const el = document.querySelector(sel);
                        if (el) {
                            const text = el.innerText.trim();
                            if (text.length > 80) return text;
                        }
                    } catch(e) {}
                }

                // Last resort: find the longest paragraph block
                const paras = document.querySelectorAll('p');
                let best = '';
                paras.forEach(p => {
                    const t = p.innerText.trim();
                    if (t.length > best.length && t.length > 100) best = t;
                });
                return best;
            }
        """)

        if summary_text and len(summary_text) > 30:
            result["nba_summary"] = summary_text[:2000]
            result["summary"] = condense_summary(summary_text)

    except Exception as e:
        log.warning(f"Summary extraction failed: {e}")

    # --- If no starters from __NEXT_DATA__, try the box score page ---
    if not result["home"]["starters"] and not result["away"]["starters"]:
        try:
            box_url = game_url + "/box-score"
            log.info(f"  Trying box score page: {box_url}")
            page.goto(box_url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(3000)

            starters = page.evaluate("""
                () => {
                    const nd = document.getElementById('__NEXT_DATA__');
                    if (!nd) return null;
                    try {
                        const json = JSON.parse(nd.textContent);
                        const game = json?.props?.pageProps?.game;
                        if (!game) return null;

                        const extract = (team) => (team?.players || [])
                            .filter(p => String(p.starter) === '1')
                            .map(p => ({
                                name: ((p.firstName || '') + ' ' + (p.familyName || '')).trim(),
                                position: p.position || '',
                                pts: p.statistics?.points || 0,
                                reb: p.statistics?.reboundsTotal || 0,
                                ast: p.statistics?.assists || 0,
                                stl: p.statistics?.steals || 0,
                                blk: p.statistics?.blocks || 0,
                            }));

                        return {
                            home: extract(game.homeTeam),
                            away: extract(game.awayTeam),
                        };
                    } catch(e) { return null; }
                }
            """)

            if starters:
                result["home"]["starters"] = starters.get("home", [])
                result["away"]["starters"] = starters.get("away", [])

        except Exception as e:
            log.warning(f"Box score page failed: {e}")

    # --- Fallback summary from stats ---
    if not result["summary"]:
        result["summary"] = generate_stats_summary(result)

    return result


# ---------------------------------------------------------------------------
# Summary condensation
# ---------------------------------------------------------------------------
def condense_summary(text: str) -> str:
    """Condense NBA.com's written summary into 1-2 sentences."""
    if not text or len(text) < 20:
        return text

    text = re.sub(r'\s+', ' ', text).strip()
    sentences = re.split(r'(?<=[.!?])\s+', text)
    if not sentences:
        return text[:200]

    priority_kw = [
        'triple-double', 'double-double', 'career-high', 'season-high',
        'points', 'scored', 'led', 'clutch', 'overtime',
        'injury', 'returned', 'debut', 'traded', 'record', 'streak',
        'first time', 'historic', 'milestone', 'ejected',
    ]

    scored = []
    for i, s in enumerate(sentences):
        lower = s.lower()
        score = sum(2 for kw in priority_kw if kw in lower)
        if i == 0:
            score += 3  # first sentence usually has the result
        scored.append((score, i, s))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:2]
    top.sort(key=lambda x: x[1])  # re-order by position

    summary = ' '.join(s for _, _, s in top)
    if len(summary) > 300:
        summary = summary[:297] + '...'

    return summary


def generate_stats_summary(game: dict) -> str:
    """Fallback summary from score data."""
    home = game.get("home", {})
    away = game.get("away", {})
    hs, as_ = home.get("score", 0), away.get("score", 0)

    if hs == 0 and as_ == 0:
        return game.get("status", "")

    winner = home if hs > as_ else away
    loser = away if hs > as_ else home
    margin = abs(hs - as_)
    hi, lo = max(hs, as_), min(hs, as_)
    wn = winner.get("name") or winner.get("tricode", "?")
    ln = loser.get("name") or loser.get("tricode", "?")

    status = game.get("status", "")
    if "OT" in status:
        return f"{wn} wins in OT, {hi}-{lo}."
    elif margin >= 20:
        return f"{wn} blowout, {hi}-{lo}."
    elif margin <= 5:
        return f"{wn} edges {ln}, {hi}-{lo}."
    else:
        return f"{wn} def. {ln}, {hi}-{lo}."


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    target = sys.argv[1] if len(sys.argv) > 1 else None
    date_label = target or datetime.now(ET).strftime("%Y-%m-%d")

    log.info(f"=== NBA Daily Scraper — {date_label} ===")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()

        slugs = get_game_slugs(page, date_label)

        results = []
        for g in slugs:
            time.sleep(1)
            data = scrape_game(page, g["slug"])
            if data:
                results.append(data)

        browser.close()

    out = {"date": date_label, "games": results}
    out_file = DATA_DIR / f"{date_label}.json"
    with open(out_file, "w") as f:
        json.dump(out, f, indent=2)
    log.info(f"Wrote {len(results)} games to {out_file}")

    dates = sorted(
        [p.stem for p in DATA_DIR.glob("*.json") if p.stem != "index"],
        reverse=True,
    )
    with open(DATA_DIR / "index.json", "w") as f:
        json.dump({"dates": dates}, f, indent=2)
    log.info(f"Index updated: {len(dates)} date(s).")


if __name__ == "__main__":
    main()
