import requests
from typing import Dict, Any, Optional

LEETCODE_GRAPHQL_URL = "https://leetcode.com/graphql"

# A query that returns solved-problem counts by difficulty plus profile ranking.
_PROFILE_QUERY = """
query userPublicProfile($username: String!) {
  matchedUser(username: $username) {
    username
    profile {
      ranking
      reputation
    }
    submitStatsGlobal {
      acSubmissionNum {
        difficulty
        count
      }
    }
  }
}
"""

# A query for contest ranking info.
_CONTEST_QUERY = """
query userContestRankingInfo($username: String!) {
  userContestRanking(username: $username) {
    attendedContestsCount
    rating
    globalRanking
    topPercentage
  }
}
"""

_HEADERS = {
    "Content-Type": "application/json",
    # LeetCode rejects requests without a browser-like Referer / User-Agent.
    "Referer": "https://leetcode.com",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}


def _post(query: str, username: str) -> Optional[Dict[str, Any]]:
    """Run a GraphQL query against LeetCode and return the `data` object."""
    resp = requests.post(
        LEETCODE_GRAPHQL_URL,
        json={"query": query, "variables": {"username": username}},
        headers=_HEADERS,
        timeout=15,
    )
    if resp.status_code != 200:
        return None
    payload = resp.json()
    return payload.get("data")


def fetch_leetcode_data(username: str) -> Optional[Dict[str, Any]]:
    """Fetch real LeetCode profile data for a user.

    Returns a dict matching the frontend `LeetcodeData` shape, or ``None``
    when the user does not exist / data could not be retrieved.
    """
    username = (username or "").strip()
    if not username:
        return None

    try:
        data = _post(_PROFILE_QUERY, username)
    except Exception as e:
        print(f"Error fetching LeetCode data for {username}: {e}")
        return None

    if not data:
        return None

    matched = data.get("matchedUser")
    if not matched:
        # Unknown username – LeetCode returns matchedUser: null.
        return None

    # Parse solved counts by difficulty.
    counts = {"All": 0, "Easy": 0, "Medium": 0, "Hard": 0}
    stats = (matched.get("submitStatsGlobal") or {}).get("acSubmissionNum") or []
    for item in stats:
        difficulty = item.get("difficulty")
        if difficulty in counts:
            counts[difficulty] = item.get("count", 0)

    profile = matched.get("profile") or {}

    result: Dict[str, Any] = {
        "username": matched.get("username", username),
        "total_solved": counts["All"],
        "easy_solved": counts["Easy"],
        "medium_solved": counts["Medium"],
        "hard_solved": counts["Hard"],
        "ranking": profile.get("ranking"),
        "reputation": profile.get("reputation"),
    }

    # Best-effort contest info (non-fatal if it fails).
    try:
        contest_data = _post(_CONTEST_QUERY, username)
        contest = (contest_data or {}).get("userContestRanking")
        if contest:
            result["contest_rating"] = (
                round(contest["rating"]) if contest.get("rating") is not None else None
            )
            result["contests_attended"] = contest.get("attendedContestsCount")
            result["top_percentage"] = contest.get("topPercentage")
    except Exception as e:
        print(f"Error fetching LeetCode contest data for {username}: {e}")

    return result
