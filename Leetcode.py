import httpx
import asyncio

async def fetch_leetcode_data(username: str) -> dict:
    """
    Uses LeetCode's public GraphQL endpoint with proper session handling.
    Falls back to alfa-leetcode-api (open proxy) if direct call fails.
    """

    # Method 1: Try direct GraphQL with a fresh session (get CSRF first)
    try:
        result = await _fetch_direct(username)
        if result and not result.get("error"):
            return result
    except Exception:
        pass

    # Method 2: Use alfa-leetcode-api — a public open proxy for LeetCode
    try:
        result = await _fetch_via_proxy(username)
        if result and not result.get("error"):
            return result
    except Exception as e:
        pass

    return {"error": f"Could not fetch LeetCode data for '{username}'. The account may be private or not exist.", "username": username}


async def _fetch_direct(username: str) -> dict:
    async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
        # Step 1: get a real session + csrf token
        await client.get("https://leetcode.com/", headers={"User-Agent": "Mozilla/5.0"})
        csrf = client.cookies.get("csrftoken", "")

        query = """
        query ($username: String!) {
          matchedUser(username: $username) {
            username
            submitStats: submitStatsGlobal {
              acSubmissionNum { difficulty count }
            }
            profile { ranking reputation }
            badges { id }
          }
          userContestRanking(username: $username) {
            attendedContestsCount rating globalRanking topPercentage
          }
        }
        """
        resp = await client.post(
            "https://leetcode.com/graphql",
            json={"query": query, "variables": {"username": username}},
            headers={
                "Content-Type": "application/json",
                "Referer": "https://leetcode.com",
                "Origin": "https://leetcode.com",
                "x-csrftoken": csrf,
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/121 Safari/537.36",
            },
        )
        body = resp.json()
        if "errors" in body:
            raise Exception(body["errors"][0].get("message", "GraphQL error"))
        user = body.get("data", {}).get("matchedUser")
        if not user:
            raise Exception("User not found")
        return _parse_graphql(user, body.get("data", {}).get("userContestRanking"))


async def _fetch_via_proxy(username: str) -> dict:
    """alfa-leetcode-api is an open-source public proxy for LeetCode stats"""
    base = "https://alfa-leetcode-api.onrender.com"
    async with httpx.AsyncClient(timeout=20) as client:
        # Get basic stats + contest info in parallel
        basic, contest = await asyncio.gather(
            client.get(f"{base}/{username}"),
            client.get(f"{base}/userContestRankingInfo/{username}"),
            return_exceptions=True
        )

        if isinstance(basic, Exception) or basic.status_code != 200:
            raise Exception("Proxy unavailable")

        b = basic.json()
        if b.get("errors") or not b.get("totalSolved") and b.get("totalSolved") != 0:
            raise Exception("User not found via proxy")

        c = {}
        if not isinstance(contest, Exception) and contest.status_code == 200:
            c = contest.json() or {}

        return {
            "username": username,
            "total_solved": b.get("totalSolved", 0),
            "easy_solved": b.get("easySolved", 0),
            "medium_solved": b.get("mediumSolved", 0),
            "hard_solved": b.get("hardSolved", 0),
            "ranking": b.get("ranking", 0),
            "reputation": b.get("reputation", 0),
            "badges": b.get("totalActiveDays", 0),  # proxy field
            "contest_rating": round(c.get("contestRating") or 0),
            "contests_attended": c.get("contestAttend", 0),
            "top_percentage": round(c.get("contestTopPercentage") or 0, 1),
        }


def _parse_graphql(user: dict, contest: dict) -> dict:
    stats = user.get("submitStats", {}).get("acSubmissionNum", [])
    solved = {s["difficulty"]: s["count"] for s in stats}
    contest = contest or {}
    profile = user.get("profile", {})
    return {
        "username": user["username"],
        "total_solved": solved.get("All", 0),
        "easy_solved": solved.get("Easy", 0),
        "medium_solved": solved.get("Medium", 0),
        "hard_solved": solved.get("Hard", 0),
        "ranking": profile.get("ranking", 0),
        "reputation": profile.get("reputation", 0),
        "badges": len(user.get("badges", [])),
        "contest_rating": round(contest.get("rating") or 0),
        "contests_attended": contest.get("attendedContestsCount", 0),
        "top_percentage": round(contest.get("topPercentage") or 0, 1),
    }