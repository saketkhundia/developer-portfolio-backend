import httpx
from datetime import datetime, timezone

async def fetch_contributions(username: str) -> dict:
    """
    Fetches GitHub contribution data via GitHub GraphQL API.
    Requires GITHUB_TOKEN env var (read:user scope is enough).
    """
    import os
    token = os.getenv("GITHUB_TOKEN", "")
    if not token:
        return {"error": "GITHUB_TOKEN not set", "username": username}

    query = """
    query($login: String!) {
      user(login: $login) {
        contributionsCollection {
          contributionCalendar {
            totalContributions
            weeks {
              contributionDays {
                date
                contributionCount
                contributionLevel
              }
            }
          }
        }
      }
    }
    """

    level_map = {
        "NONE":           0,
        "FIRST_QUARTILE": 1,
        "SECOND_QUARTILE":2,
        "THIRD_QUARTILE": 3,
        "FOURTH_QUARTILE":4,
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.github.com/graphql",
                json={"query": query, "variables": {"login": username}},
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "DevIQ/1.0",
                },
            )
            if resp.status_code != 200:
                raise Exception(f"GitHub API HTTP {resp.status_code}")

            body = resp.json()
            if "errors" in body:
                raise Exception(body["errors"][0]["message"])

            calendar = (
                body["data"]["user"]
                ["contributionsCollection"]
                ["contributionCalendar"]
            )

        # Flatten weeks → days
        contributions = []
        for week in calendar["weeks"]:
            for day in week["contributionDays"]:
                contributions.append({
                    "date":  day["date"],
                    "count": day["contributionCount"],
                    "level": level_map.get(day["contributionLevel"], 0),
                })

        contributions.sort(key=lambda x: x["date"])

        # Longest streak
        longest_streak = temp = 0
        for d in contributions:
            if d["count"] > 0:
                temp += 1
                longest_streak = max(longest_streak, temp)
            else:
                temp = 0

        # Current streak — skip today if it still has 0 (day not over)
        today_str = datetime.now(timezone.utc).date().isoformat()
        streak_days = contributions[:]
        if streak_days and streak_days[-1]["date"] == today_str and streak_days[-1]["count"] == 0:
            streak_days = streak_days[:-1]  # don't penalise an in-progress day

        current_streak = 0
        for d in reversed(streak_days):
            if d["count"] > 0:
                current_streak += 1
            else:
                break

        total_last_year = calendar["totalContributions"]
        busiest_day = max(contributions, key=lambda x: x["count"], default={})

        return {
            "username":       username,
            "contributions":  contributions,
            "total_last_year":total_last_year,
            "current_streak": current_streak,
            "longest_streak": longest_streak,
            "busiest_day":    busiest_day,
        }

    except Exception as e:
        return {"error": str(e), "username": username}