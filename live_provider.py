import os
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import requests

IST = timezone(timedelta(hours=5, minutes=30), "IST")


def env_int(name, default, minimum=None):
    raw_value = os.getenv(name, "").strip()
    try:
        parsed = int(raw_value) if raw_value else default
    except ValueError:
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    return parsed


def env_float(name, default):
    raw_value = os.getenv(name, "").strip()
    try:
        return float(raw_value) if raw_value else default
    except ValueError:
        return default


LIVE_SCORE_PROVIDER = os.getenv("LIVE_SCORE_PROVIDER", "sportmonks").strip().lower()
SPORTMONKS_API_TOKEN = os.getenv("SPORTMONKS_API_TOKEN", "").strip()
SPORTMONKS_BASE_URL = os.getenv("SPORTMONKS_BASE_URL", "https://cricket.sportmonks.com/api/v2.0").rstrip("/")
SPORTMONKS_LEAGUE_ID = os.getenv("SPORTMONKS_LEAGUE_ID", "").strip()
SPORTMONKS_SEASON_ID = os.getenv("SPORTMONKS_SEASON_ID", "").strip()
SPORTMONKS_STAGE_ID = os.getenv("SPORTMONKS_STAGE_ID", "").strip()
SPORTMONKS_FIXTURE_ID = os.getenv("SPORTMONKS_FIXTURE_ID", "").strip()
LIVE_SCORE_REFRESH_SECONDS = env_int("LIVE_SCORE_REFRESH_SECONDS", 20, minimum=10)
LIVE_SCORE_CACHE_TTL = env_int("LIVE_SCORE_CACHE_TTL", max(10, LIVE_SCORE_REFRESH_SECONDS - 5), minimum=5)
LIVE_SCORE_TIMEOUT_SECONDS = env_float("LIVE_SCORE_TIMEOUT_SECONDS", 8.0)
RECENT_MATCH_COUNT = env_int("RECENT_MATCH_COUNT", 6, minimum=1)
UPCOMING_MATCH_COUNT = env_int("UPCOMING_MATCH_COUNT", 6, minimum=1)
FIXTURE_LOOKBACK_DAYS = env_int("FIXTURE_LOOKBACK_DAYS", 21, minimum=1)
FIXTURE_LOOKAHEAD_DAYS = env_int("FIXTURE_LOOKAHEAD_DAYS", 21, minimum=1)

LIVE_SCORE_CACHE = {"expires_at": 0.0, "payload": None}
FULL_DASHBOARD_CACHE = {"expires_at": 0.0, "payload": None}


def get_now():
    return datetime.now(IST)


def parse_provider_datetime(raw_value):
    if not raw_value:
        return None
    normalized = raw_value.replace(" ", "T").replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(IST)


def format_match_time(raw_value):
    parsed = parse_provider_datetime(raw_value)
    if not parsed:
        return "Time to be announced"
    return parsed.strftime("%d %b, %I:%M %p IST")


def as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def overs_to_balls(overs_value):
    overs_float = as_float(overs_value)
    if overs_float is None:
        return None
    whole_overs = int(overs_float)
    partial_balls = int(round((overs_float - whole_overs) * 10))
    return whole_overs * 6 + partial_balls


def safe_team_short(team_data, fallback):
    if not isinstance(team_data, dict):
        return fallback
    return team_data.get("code") or fallback


def safe_team_name(team_data, fallback_short, team_name_map):
    if not isinstance(team_data, dict):
        return team_name_map.get(fallback_short, fallback_short)
    return team_data.get("name") or team_name_map.get(fallback_short, fallback_short)


def build_feed_state(source, configured, using_live_data, has_live_match, message):
    return {
        "source": source,
        "configured": configured,
        "using_live_data": using_live_data,
        "has_live_match": has_live_match,
        "poll_interval_seconds": LIVE_SCORE_REFRESH_SECONDS,
        "message": message,
    }


def build_demo_live_payload(demo_live_match, message=None):
    now = get_now()
    next_match = deepcopy(demo_live_match)
    next_match["status"] = "Scheduled"
    next_match["series_note"] = "Upcoming fixture"
    next_match["status_note"] = "Match has not started yet. Showing a scheduled placeholder until a real live feed is configured."
    next_match["run_rate"] = "--"
    next_match["required_rate"] = "--"
    next_match["partnership"] = "Live metrics will appear once the match begins."
    next_match["win_probability"] = {"home": 50, "away": 50}
    next_match["batters"] = []
    next_match["bowlers"] = []
    next_match["recent_over"] = []
    next_match["innings"] = [
        {"team": next_match["away_short"], "score": "--/--", "overs": "--", "state": "pending"},
        {"team": next_match["home_short"], "score": "--/--", "overs": "--", "state": "pending"},
    ]
    return {
        "generated_at": now.isoformat(),
        "display_date": now.strftime("%A, %d %B %Y"),
        "last_updated": now.strftime("%I:%M %p IST"),
        "live_match": next_match,
        "live_feed": build_feed_state(
            source="demo",
            configured=False,
            using_live_data=False,
            has_live_match=False,
            message=message or "No live API token is configured, so the page shows the next scheduled match instead of a fake live score.",
        ),
    }


def build_no_live_match_payload(upcoming_matches, team_name_map):
    next_match = upcoming_matches[0]
    home_short, away_short = [value.strip() for value in next_match["fixture"].split("vs")]
    now = get_now()
    return {
        "generated_at": now.isoformat(),
        "display_date": now.strftime("%A, %d %B %Y"),
        "last_updated": now.strftime("%I:%M %p IST"),
        "live_match": {
            "match_no": next_match["id"],
            "status": "No Live Match",
            "series_note": f"Next up: Match {next_match['id']}",
            "home_name": team_name_map.get(home_short, home_short),
            "home_short": home_short,
            "away_name": team_name_map.get(away_short, away_short),
            "away_short": away_short,
            "venue": next_match["venue"],
            "start_time": f"{next_match['date']} | {next_match['time']}",
            "toss": "Toss information will appear once the match begins.",
            "innings": [
                {"team": home_short, "score": "--/--", "overs": "--", "state": "pending"},
                {"team": away_short, "score": "--/--", "overs": "--", "state": "pending"},
            ],
            "status_note": "There is no live IPL match right now. Showing the next scheduled fixture.",
            "run_rate": "--",
            "required_rate": "--",
            "partnership": next_match["note"],
            "win_probability": {"home": 50, "away": 50},
            "batters": [],
            "bowlers": [],
            "recent_over": [],
        },
        "live_feed": build_feed_state(
            source="sportmonks",
            configured=True,
            using_live_data=True,
            has_live_match=False,
            message="Real provider connected. No live IPL fixture is available at this moment.",
        ),
    }


def request_sportmonks(endpoint, params=None):
    query = dict(params or {})
    query["api_token"] = SPORTMONKS_API_TOKEN
    response = requests.get(
        f"{SPORTMONKS_BASE_URL}/{endpoint.lstrip('/')}",
        params=query,
        timeout=LIVE_SCORE_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    body = response.json()
    return body.get("data")


def normalize_bool(value):
    return value in (True, 1, "1", "true", "True")


def team_from_fixture(fixture, side):
    if side == "home":
        return fixture.get("localteam") or {}, fixture.get("localteam_id")
    return fixture.get("visitorteam") or {}, fixture.get("visitorteam_id")


def latest_team_runs(runs, team_id):
    team_rows = [row for row in (runs or []) if row.get("team_id") == team_id]
    if not team_rows:
        return None
    team_rows.sort(key=lambda row: (row.get("inning", 0), row.get("id", 0)))
    return team_rows[-1]


def format_runs_row(runs_row):
    if not runs_row:
        return "--/--"
    score = runs_row.get("score")
    wickets = runs_row.get("wickets")
    if score is None or wickets is None:
        return "--/--"
    return f"{score}/{wickets}"


def format_fixture_date(raw_value):
    parsed = parse_provider_datetime(raw_value)
    if not parsed:
        return "Date pending"
    return parsed.strftime("%b %d, %Y")


def format_fixture_time(raw_value):
    parsed = parse_provider_datetime(raw_value)
    if not parsed:
        return "Time pending"
    return parsed.strftime("%I:%M %p IST")


def fixture_sort_key(fixture):
    parsed = parse_provider_datetime(fixture.get("starting_at"))
    return parsed or get_now()


def fixture_is_upcoming(fixture):
    parsed = parse_provider_datetime(fixture.get("starting_at"))
    status = str(fixture.get("status") or "").lower()
    pending_tokens = ("ns", "scheduled", "not started", "delayed", "int", "stumps", "tba")
    if any(token == status for token in pending_tokens):
        return True
    if parsed:
        return parsed > get_now() and not normalize_bool(fixture.get("live"))
    return False


def fixture_is_complete(fixture):
    status = str(fixture.get("status") or "").lower()
    finished_tokens = ("finished", "result", "aban.", "abandoned", "cancelled", "cancl.", "draw", "no result")
    if any(token in status for token in finished_tokens):
        return True
    return bool(fixture.get("winner_team_id")) or (not normalize_bool(fixture.get("live")) and bool(fixture.get("note")))


def build_team_map_from_season(season_payload, fallback_map):
    team_map = dict(fallback_map)
    id_map = {}
    teams = (season_payload or {}).get("teams") or []
    for team in teams:
        code = team.get("code") or team.get("name") or str(team.get("id"))
        name = team.get("name") or code
        team_map[code] = name
        id_map[team.get("id")] = {"code": code, "name": name}
    return team_map, id_map


def extract_active_batting_team_id(batting_rows):
    for batter in batting_rows or []:
        if batter.get("active") in (True, 1, "1", "true", "True"):
            return batter.get("team_id")
    return None


def build_innings(runs, batting_team_id, local_team_id, visitor_team_id, local_short, visitor_short):
    innings = []
    ordered_runs = sorted(runs or [], key=lambda row: (row.get("inning", 0), row.get("id", 0)))
    if not ordered_runs:
        return [
            {"team": local_short, "score": "--/--", "overs": "--", "state": "pending"},
            {"team": visitor_short, "score": "--/--", "overs": "--", "state": "pending"},
        ]

    for row in ordered_runs:
        team_id = row.get("team_id")
        short_name = local_short if team_id == local_team_id else visitor_short if team_id == visitor_team_id else "TEAM"
        score = row.get("score")
        wickets = row.get("wickets")
        overs = row.get("overs")
        innings.append(
            {
                "team": short_name,
                "score": f"{score}/{wickets}" if score is not None and wickets is not None else "--/--",
                "overs": str(overs) if overs is not None else "--",
                "state": "batting" if team_id == batting_team_id else "completed",
            }
        )

    if batting_team_id is None and innings:
        innings[-1]["state"] = "batting"

    return innings


def build_batters(batting_rows):
    rows = [row for row in (batting_rows or []) if row.get("active") in (True, 1, "1", "true", "True")]
    if not rows:
        rows = sorted(batting_rows or [], key=lambda row: row.get("score") or 0, reverse=True)[:2]

    batters = []
    for index, row in enumerate(rows[:2], start=1):
        batter_data = row.get("batsman") or {}
        batters.append(
            {
                "name": batter_data.get("fullname") or batter_data.get("lastname") or "Batter",
                "runs": row.get("score") or 0,
                "balls": row.get("ball") or row.get("balls") or 0,
                "fours": row.get("four_x") or row.get("fours") or 0,
                "sixes": row.get("six_x") or row.get("sixes") or 0,
                "tag": "Set batter" if index == 1 else "Partner",
            }
        )
    return batters


def build_bowlers(bowling_rows):
    rows = [row for row in (bowling_rows or []) if row.get("active") in (True, 1, "1", "true", "True")]
    if not rows:
        rows = sorted(bowling_rows or [], key=lambda row: as_float(row.get("overs")) or 0, reverse=True)[:2]

    bowlers = []
    for row in rows[:2]:
        bowler_data = row.get("bowler") or {}
        bowlers.append(
            {
                "name": bowler_data.get("fullname") or bowler_data.get("lastname") or "Bowler",
                "overs": str(row.get("overs") or "--"),
                "runs": row.get("runs") or 0,
                "wickets": row.get("wickets") or 0,
            }
        )
    return bowlers


def build_recent_over(balls):
    if not balls:
        return []

    ordered = sorted(balls, key=lambda row: row.get("id", 0))
    recent = []
    for ball in ordered[-6:]:
        score = ball.get("score") if ball.get("score") is not None else ball.get("result")
        if score is None:
            score = ball.get("ball")
        recent.append(str(score))
    return recent


def estimate_required_rate(innings):
    if len(innings) < 2:
        return "--"
    first_score = innings[0]["score"].split("/")[0]
    second_score = innings[1]["score"].split("/")[0]
    current_overs = innings[1]["overs"]
    target = as_float(first_score)
    current = as_float(second_score)
    balls_used = overs_to_balls(current_overs)
    if target is None or current is None or balls_used is None:
        return "--"
    balls_left = max(0, 120 - balls_used)
    runs_needed = max(0, int(target + 1 - current))
    if balls_left == 0:
        return "--"
    return f"{(runs_needed / balls_left) * 6:.2f}"


def estimate_win_probability(innings):
    if len(innings) < 2:
        return {"home": 50, "away": 50}
    first_score = as_float(innings[0]["score"].split("/")[0])
    second_score = as_float(innings[1]["score"].split("/")[0])
    if first_score is None or second_score is None:
        return {"home": 50, "away": 50}
    ratio = second_score / max(first_score + 1, 1)
    home = int(max(10, min(90, round(ratio * 100))))
    return {"home": home, "away": 100 - home}


def build_sportmonks_live_payload(team_name_map, upcoming_matches):
    include = "localteam,visitorteam,venue,runs,batting.batsman,bowling.bowler,balls"
    if SPORTMONKS_FIXTURE_ID:
        fixture = request_sportmonks(f"fixtures/{SPORTMONKS_FIXTURE_ID}", {"include": include})
    else:
        params = {"include": include}
        if SPORTMONKS_LEAGUE_ID:
            params["filter[league_id]"] = SPORTMONKS_LEAGUE_ID
        fixtures = request_sportmonks("livescores", params) or []
        fixture = fixtures[0] if fixtures else None

    if not fixture:
        return build_no_live_match_payload(upcoming_matches, team_name_map)

    local_team = fixture.get("localteam") or {}
    visitor_team = fixture.get("visitorteam") or {}
    local_short = safe_team_short(local_team, "HOME")
    visitor_short = safe_team_short(visitor_team, "AWAY")
    batting_rows = fixture.get("batting") or []
    bowling_rows = fixture.get("bowling") or []
    runs = fixture.get("runs") or []
    active_batting_team_id = extract_active_batting_team_id(batting_rows)
    innings = build_innings(
        runs=runs,
        batting_team_id=active_batting_team_id,
        local_team_id=fixture.get("localteam_id"),
        visitor_team_id=fixture.get("visitorteam_id"),
        local_short=local_short,
        visitor_short=visitor_short,
    )
    batters = build_batters(batting_rows)
    bowlers = build_bowlers(bowling_rows)
    recent_over = build_recent_over(fixture.get("balls") or [])
    current_overs = innings[-1]["overs"] if innings else "--"
    current_score = innings[-1]["score"].split("/")[0] if innings else None
    balls_used = overs_to_balls(current_overs)
    run_rate = "--"
    if balls_used and current_score is not None:
        current_runs = as_float(current_score)
        if current_runs is not None:
            run_rate = f"{(current_runs / balls_used) * 6:.2f}"

    partnership_runs = sum(player["runs"] for player in batters)
    partnership_balls = sum(player["balls"] for player in batters)
    partnership = "Awaiting partnership data"
    if batters:
        names = " / ".join(player["name"].split(" ")[-1] for player in batters)
        partnership = f"{names}: {partnership_runs} runs in {partnership_balls} balls"

    note = fixture.get("note") or fixture.get("status") or "Live score updating"
    now = get_now()
    return {
        "generated_at": now.isoformat(),
        "display_date": now.strftime("%A, %d %B %Y"),
        "last_updated": now.strftime("%I:%M %p IST"),
        "live_match": {
            "match_no": fixture.get("round") or fixture.get("id"),
            "status": fixture.get("status") or "Live",
            "series_note": f"Fixture {fixture.get('id')}",
            "home_name": safe_team_name(local_team, local_short, team_name_map),
            "home_short": local_short,
            "away_name": safe_team_name(visitor_team, visitor_short, team_name_map),
            "away_short": visitor_short,
            "venue": (fixture.get("venue") or {}).get("name") or "Venue update pending",
            "start_time": format_match_time(fixture.get("starting_at")),
            "toss": note,
            "innings": innings,
            "status_note": note,
            "run_rate": run_rate,
            "required_rate": estimate_required_rate(innings),
            "partnership": partnership,
            "win_probability": estimate_win_probability(innings),
            "batters": batters,
            "bowlers": bowlers,
            "recent_over": recent_over,
        },
        "live_feed": build_feed_state(
            source="sportmonks",
            configured=True,
            using_live_data=True,
            has_live_match=bool(fixture.get("live", True)),
            message="Real live data is being served from your configured cricket provider.",
        ),
    }


def fetch_season_context_from_provider():
    season_id = SPORTMONKS_SEASON_ID
    if season_id:
        season = request_sportmonks(f"seasons/{season_id}", {"include": "teams"})
        return season_id, season

    params = {
        "include": "localteam,visitorteam,venue,runs,manofmatch",
        "sort": "starting_at",
    }
    if SPORTMONKS_LEAGUE_ID:
        params["filter[league_id]"] = SPORTMONKS_LEAGUE_ID
    if SPORTMONKS_STAGE_ID:
        params["filter[stage_id]"] = SPORTMONKS_STAGE_ID

    now = get_now()
    start_window = (now - timedelta(days=FIXTURE_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    end_window = (now + timedelta(days=FIXTURE_LOOKAHEAD_DAYS)).strftime("%Y-%m-%d")
    params["filter[starts_between]"] = f"{start_window},{end_window}"
    fixtures = request_sportmonks("fixtures", params) or []
    if not fixtures:
        return "", None

    season_id = str(fixtures[0].get("season_id") or "")
    if not season_id:
        return "", None
    season = request_sportmonks(f"seasons/{season_id}", {"include": "teams"})
    return season_id, season


def fetch_provider_fixtures(season_id):
    now = get_now()
    params = {
        "include": "localteam,visitorteam,venue,runs,manofmatch",
        "sort": "starting_at",
        "filter[starts_between]": f"{(now - timedelta(days=FIXTURE_LOOKBACK_DAYS)).strftime('%Y-%m-%d')},{(now + timedelta(days=FIXTURE_LOOKAHEAD_DAYS)).strftime('%Y-%m-%d')}",
    }
    if season_id:
        params["filter[season_id]"] = season_id
    if SPORTMONKS_LEAGUE_ID:
        params["filter[league_id]"] = SPORTMONKS_LEAGUE_ID
    if SPORTMONKS_STAGE_ID:
        params["filter[stage_id]"] = SPORTMONKS_STAGE_ID
    return request_sportmonks("fixtures", params) or []


def fixture_to_recent_match(fixture, team_id_map):
    home_team, home_team_id = team_from_fixture(fixture, "home")
    away_team, away_team_id = team_from_fixture(fixture, "away")
    home_short = safe_team_short(home_team, team_id_map.get(home_team_id, {}).get("code", "HOME"))
    away_short = safe_team_short(away_team, team_id_map.get(away_team_id, {}).get("code", "AWAY"))
    man_of_match = fixture.get("manofmatch") or {}
    runs = fixture.get("runs") or []
    return {
        "match_no": fixture.get("round") or fixture.get("id"),
        "date": format_fixture_date(fixture.get("starting_at")),
        "venue": (fixture.get("venue") or {}).get("name") or "Venue pending",
        "home_short": home_short,
        "away_short": away_short,
        "home_score": format_runs_row(latest_team_runs(runs, home_team_id)),
        "away_score": format_runs_row(latest_team_runs(runs, away_team_id)),
        "result": fixture.get("note") or fixture.get("status") or "Result pending",
        "player_of_match": man_of_match.get("fullname") or man_of_match.get("lastname") or "TBA",
        "key_moment": fixture.get("note") or "Match summary will appear here.",
    }


def fixture_to_upcoming_match(fixture, team_id_map):
    home_team, home_team_id = team_from_fixture(fixture, "home")
    away_team, away_team_id = team_from_fixture(fixture, "away")
    home_short = safe_team_short(home_team, team_id_map.get(home_team_id, {}).get("code", "HOME"))
    away_short = safe_team_short(away_team, team_id_map.get(away_team_id, {}).get("code", "AWAY"))
    return {
        "id": fixture.get("id"),
        "fixture": f"{home_short} vs {away_short}",
        "date": format_fixture_date(fixture.get("starting_at")),
        "time": format_fixture_time(fixture.get("starting_at")),
        "venue": (fixture.get("venue") or {}).get("name") or "Venue pending",
        "note": fixture.get("round") or fixture.get("type") or "Upcoming fixture",
    }


def build_points_table_from_standings(standings_rows, team_id_map):
    table = []
    ordered_rows = sorted(standings_rows or [], key=lambda item: item.get("position") or 9999)
    for index, row in enumerate(ordered_rows, start=1):
        team_info = team_id_map.get(row.get("team_id"), {})
        code = team_info.get("code") or f"TEAM {row.get('team_id')}"
        name = team_info.get("name") or code
        won = row.get("won") or 0
        lost = row.get("lost") or 0
        no_result = row.get("draw") or row.get("drawn") or row.get("noresult") or row.get("nr") or 0
        played = row.get("played") or (won + lost + no_result)
        recent_form = row.get("recent_form") or []
        form = recent_form[0] if recent_form else ("W" if won >= lost else "L" if lost > won else "-")
        net_run_rate = as_float(row.get("netto_run_rate"))
        if net_run_rate is None:
            net_run_rate = as_float(row.get("netrr"))
        table.append(
            {
                "position": row.get("position") or index,
                "name": name,
                "short_name": code,
                "played": played,
                "won": won,
                "lost": lost,
                "no_result": no_result,
                "points": row.get("points") or 0,
                "nrr": f"{(net_run_rate if net_run_rate is not None else 0):+.3f}",
                "form": form,
                "last_result": "Recent form: " + " ".join(recent_form) if recent_form else "Provider standings update",
            }
        )
    return table


def fetch_provider_dashboard_data(demo_live_match, fallback_recent_matches, fallback_upcoming_matches, fallback_points_table, fallback_team_name_map):
    live_payload = fetch_live_score_payload(
        demo_live_match=demo_live_match,
        team_name_map=fallback_team_name_map,
        upcoming_matches=fallback_upcoming_matches,
    )

    if not SPORTMONKS_API_TOKEN or LIVE_SCORE_PROVIDER != "sportmonks":
        return {
            "live_payload": live_payload,
            "recent_matches": fallback_recent_matches,
            "upcoming_matches": fallback_upcoming_matches,
            "points_table": fallback_points_table,
        }

    now_monotonic = time.monotonic()
    cached = FULL_DASHBOARD_CACHE["payload"]
    if cached and FULL_DASHBOARD_CACHE["expires_at"] > now_monotonic:
        return deepcopy(cached)

    season_id = ""
    try:
        season_id, season_payload = fetch_season_context_from_provider()
        provider_team_map, team_id_map = build_team_map_from_season(season_payload, fallback_team_name_map)
        fixtures = fetch_provider_fixtures(season_id)
        completed = [fixture for fixture in fixtures if fixture_is_complete(fixture)]
        upcoming = [fixture for fixture in fixtures if fixture_is_upcoming(fixture)]
        completed.sort(key=fixture_sort_key, reverse=True)
        upcoming.sort(key=fixture_sort_key)

        recent_matches = [fixture_to_recent_match(fixture, team_id_map) for fixture in completed[:RECENT_MATCH_COUNT]] or fallback_recent_matches
        upcoming_matches = [fixture_to_upcoming_match(fixture, team_id_map) for fixture in upcoming[:UPCOMING_MATCH_COUNT]] or fallback_upcoming_matches

        points_table = fallback_points_table
        if season_id:
            standings_endpoint = f"standings/stage/{SPORTMONKS_STAGE_ID}" if SPORTMONKS_STAGE_ID else f"standings/season/{season_id}"
            standings_rows = request_sportmonks(standings_endpoint) or []
            built_table = build_points_table_from_standings(standings_rows, team_id_map)
            if built_table:
                points_table = built_table

        payload = {
            "live_payload": fetch_live_score_payload(
                demo_live_match=demo_live_match,
                team_name_map=provider_team_map,
                upcoming_matches=upcoming_matches,
                force_refresh=True,
            ),
            "recent_matches": recent_matches,
            "upcoming_matches": upcoming_matches,
            "points_table": points_table,
        }
    except (requests.RequestException, ValueError, KeyError):
        payload = {
            "live_payload": live_payload,
            "recent_matches": fallback_recent_matches,
            "upcoming_matches": fallback_upcoming_matches,
            "points_table": fallback_points_table,
        }

    FULL_DASHBOARD_CACHE["payload"] = deepcopy(payload)
    FULL_DASHBOARD_CACHE["expires_at"] = now_monotonic + LIVE_SCORE_CACHE_TTL
    return payload


def fetch_live_score_payload(demo_live_match, team_name_map, upcoming_matches, force_refresh=False):
    now_monotonic = time.monotonic()
    cached = LIVE_SCORE_CACHE["payload"]
    if not force_refresh and cached and LIVE_SCORE_CACHE["expires_at"] > now_monotonic:
        return deepcopy(cached)

    if LIVE_SCORE_PROVIDER != "sportmonks" or not SPORTMONKS_API_TOKEN:
        payload = build_demo_live_payload(demo_live_match)
    else:
        try:
            payload = build_sportmonks_live_payload(team_name_map, upcoming_matches)
        except (requests.RequestException, ValueError, KeyError) as exc:
            payload = build_demo_live_payload(
                demo_live_match,
                message=f"Live provider could not be reached just now. Showing demo data instead. Details: {exc}",
            )

    LIVE_SCORE_CACHE["payload"] = deepcopy(payload)
    LIVE_SCORE_CACHE["expires_at"] = now_monotonic + LIVE_SCORE_CACHE_TTL
    return payload
