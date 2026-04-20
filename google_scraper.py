import html
import re
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import requests

IST = timezone(timedelta(hours=5, minutes=30), "IST")
GOOGLE_SEARCH_URL = "https://www.google.com/search"
GOOGLE_CACHE_TTL = 300

GOOGLE_CACHE = {"expires_at": 0.0, "payload": None}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
}

COOKIES = {
    "CONSENT": "YES+cb.20210328-17-p0.en+FX+917",
}


def now_ist():
    return datetime.now(IST)


def parse_schedule_datetime(date_value, time_value):
    try:
        parsed = datetime.strptime(f"{date_value} {time_value}", "%B %d, %Y %I:%M %p")
    except ValueError:
        return None
    return parsed.replace(tzinfo=IST)


def fetch_google_search_text(query):
    response = requests.get(
        GOOGLE_SEARCH_URL,
        params={"q": query, "hl": "en", "gl": "in", "gbv": "1"},
        headers=HEADERS,
        cookies=COOKIES,
        timeout=12,
    )
    response.raise_for_status()
    body = response.text
    body = re.sub(r"(?is)<script.*?>.*?</script>", " ", body)
    body = re.sub(r"(?is)<style.*?>.*?</style>", " ", body)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    body = html.unescape(body)
    return " ".join(body.split())


def find_score(text, team_tokens):
    for token in team_tokens:
        pattern = re.compile(rf"{re.escape(token)}\s+(\d+/\d+)", re.IGNORECASE)
        match = pattern.search(text)
        if match:
            return match.group(1)
    return "--/--"


def find_result_text(text):
    patterns = [
        r"([A-Za-z .]+ won by [A-Za-z0-9 ,.-]+)",
        r"([A-Za-z .]+ beat [A-Za-z .]+ by [A-Za-z0-9 ,.-]+)",
        r"((?:match|game) (?:was )?abandoned[A-Za-z0-9 ,.-]*)",
        r"((?:no result|match tied)[A-Za-z0-9 ,.-]*)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip().rstrip(",")
    return ""


def infer_fixture_status(text, scheduled_at):
    lowered = text.lower()
    result_text = find_result_text(text)
    if result_text:
        return "finished", result_text
    if "live" in lowered or "need" in lowered or "target" in lowered:
        return "live", ""
    if scheduled_at and scheduled_at > now_ist():
        return "scheduled", ""
    return "scheduled", ""


def scrape_fixture(fixture, team_name_map):
    home_name = team_name_map.get(fixture["home_short"], fixture["home_short"])
    away_name = team_name_map.get(fixture["away_short"], fixture["away_short"])
    query = f"{home_name} vs {away_name} IPL {fixture['date'].split(',')[-1].strip()} score"
    text = fetch_google_search_text(query)
    scheduled_at = parse_schedule_datetime(fixture["date"], fixture["time"])
    status, result_text = infer_fixture_status(text, scheduled_at)

    home_score = find_score(text, [home_name, fixture["home_short"]])
    away_score = find_score(text, [away_name, fixture["away_short"]])

    return {
        "id": fixture["id"],
        "home_short": fixture["home_short"],
        "away_short": fixture["away_short"],
        "home_name": home_name,
        "away_name": away_name,
        "date": fixture["date"],
        "time": fixture["time"],
        "venue": fixture["venue"],
        "status": status,
        "home_score": home_score,
        "away_score": away_score,
        "result": result_text or "Match details not available from Google yet",
    }


def build_recent_matches(scraped_fixtures):
    finished = [fixture for fixture in scraped_fixtures if fixture["status"] == "finished"]
    finished.sort(key=lambda item: item["id"], reverse=True)
    return [
        {
            "match_no": fixture["id"],
            "date": fixture["date"],
            "venue": fixture["venue"],
            "home_short": fixture["home_short"],
            "away_short": fixture["away_short"],
            "home_score": fixture["home_score"],
            "away_score": fixture["away_score"],
            "result": fixture["result"],
            "player_of_match": "Google scrape project mode",
            "key_moment": fixture["result"],
        }
        for fixture in finished
    ]


def build_upcoming_matches(scraped_fixtures):
    pending = [fixture for fixture in scraped_fixtures if fixture["status"] != "finished"]
    pending.sort(key=lambda item: item["id"])
    return [
        {
            "id": fixture["id"],
            "fixture": f"{fixture['home_short']} vs {fixture['away_short']}",
            "date": fixture["date"],
            "time": fixture["time"],
            "venue": fixture["venue"],
            "note": "Google schedule/project mode",
        }
        for fixture in pending
    ]


def build_points_table(scraped_fixtures, team_name_map):
    table = {}
    for code, name in team_name_map.items():
        table[code] = {
            "name": name,
            "short_name": code,
            "played": 0,
            "won": 0,
            "lost": 0,
            "no_result": 0,
            "points": 0,
            "nrr": "N/A",
            "form": "-",
            "last_result": "Awaiting result",
        }

    for fixture in scraped_fixtures:
        if fixture["status"] != "finished":
            continue
        home = table[fixture["home_short"]]
        away = table[fixture["away_short"]]
        home["played"] += 1
        away["played"] += 1
        home["last_result"] = fixture["result"]
        away["last_result"] = fixture["result"]

        result_text = fixture["result"].lower()
        if "abandoned" in result_text or "no result" in result_text or "tied" in result_text:
            home["no_result"] += 1
            away["no_result"] += 1
            home["points"] += 1
            away["points"] += 1
            continue

        if fixture["home_name"].lower() in result_text or fixture["home_short"].lower() in result_text:
            home["won"] += 1
            away["lost"] += 1
            home["points"] += 2
            home["form"] = "W"
            away["form"] = "L"
        elif fixture["away_name"].lower() in result_text or fixture["away_short"].lower() in result_text:
            away["won"] += 1
            home["lost"] += 1
            away["points"] += 2
            away["form"] = "W"
            home["form"] = "L"

    ordered = sorted(table.values(), key=lambda row: (-row["points"], -row["won"], row["name"]))
    for index, row in enumerate(ordered, start=1):
        row["position"] = index
    return ordered


def build_live_payload(upcoming_matches, team_name_map):
    next_match = upcoming_matches[0] if upcoming_matches else None
    now = now_ist()
    if not next_match:
        return {
            "generated_at": now.isoformat(),
            "display_date": now.strftime("%A, %d %B %Y"),
            "last_updated": now.strftime("%I:%M %p IST"),
            "live_match": {
                "match_no": "--",
                "status": "No Match Scheduled",
                "series_note": "Schedule exhausted",
                "home_name": "TBA",
                "home_short": "TBA",
                "away_name": "TBA",
                "away_short": "TBA",
                "venue": "TBA",
                "start_time": "TBA",
                "toss": "No scheduled fixture found.",
                "innings": [
                    {"team": "TBA", "score": "--/--", "overs": "--", "state": "pending"},
                    {"team": "TBA", "score": "--/--", "overs": "--", "state": "pending"},
                ],
                "status_note": "No upcoming fixture available in the tracked schedule.",
                "run_rate": "--",
                "required_rate": "--",
                "partnership": "Google scrape project mode",
                "win_probability": {"home": 50, "away": 50},
                "batters": [],
                "bowlers": [],
                "recent_over": [],
            },
            "live_feed": {
                "source": "google-scrape",
                "configured": True,
                "using_live_data": False,
                "has_live_match": False,
                "poll_interval_seconds": 120,
                "message": "Project mode: scraping Google search result pages for finished matches and schedule updates.",
            },
        }

    return {
        "generated_at": now.isoformat(),
        "display_date": now.strftime("%A, %d %B %Y"),
        "last_updated": now.strftime("%I:%M %p IST"),
        "live_match": {
            "match_no": next_match["id"],
            "status": "Scheduled",
            "series_note": "Next tracked fixture",
            "home_name": team_name_map.get(next_match["home_short"], next_match["home_short"]),
            "home_short": next_match["home_short"],
            "away_name": team_name_map.get(next_match["away_short"], next_match["away_short"]),
            "away_short": next_match["away_short"],
            "venue": next_match["venue"],
            "start_time": f"{next_match['date']} | {next_match['time']}",
            "toss": "Live score is disabled in Google scrape mode.",
            "innings": [
                {"team": next_match["away_short"], "score": "--/--", "overs": "--", "state": "pending"},
                {"team": next_match["home_short"], "score": "--/--", "overs": "--", "state": "pending"},
            ],
            "status_note": "Finished matches and points update automatically; live ball-by-ball is disabled.",
            "run_rate": "--",
            "required_rate": "--",
            "partnership": "Waiting for the scheduled start.",
            "win_probability": {"home": 50, "away": 50},
            "batters": [],
            "bowlers": [],
            "recent_over": [],
        },
        "live_feed": {
            "source": "google-scrape",
            "configured": True,
            "using_live_data": False,
            "has_live_match": False,
            "poll_interval_seconds": 120,
            "message": "Project mode: scraping Google search result pages for finished matches and schedule updates.",
        },
    }


def fetch_google_dashboard_data(match_schedule, team_name_map, fallback_recent, fallback_upcoming, fallback_points):
    now_monotonic = time.monotonic()
    cached = GOOGLE_CACHE["payload"]
    if cached and GOOGLE_CACHE["expires_at"] > now_monotonic:
        return deepcopy(cached)

    scraped_fixtures = [scrape_fixture(fixture, team_name_map) for fixture in match_schedule]
    recent_matches = build_recent_matches(scraped_fixtures) or fallback_recent
    upcoming_matches = build_upcoming_matches(scraped_fixtures) or fallback_upcoming
    points_table = build_points_table(scraped_fixtures, team_name_map) or fallback_points
    live_payload = build_live_payload(
        upcoming_matches=[fixture for fixture in match_schedule if fixture["id"] in {row["id"] for row in upcoming_matches}],
        team_name_map=team_name_map,
    )

    payload = {
        "live_payload": live_payload,
        "recent_matches": recent_matches,
        "upcoming_matches": upcoming_matches,
        "points_table": points_table,
    }
    GOOGLE_CACHE["payload"] = deepcopy(payload)
    GOOGLE_CACHE["expires_at"] = now_monotonic + GOOGLE_CACHE_TTL
    return payload
