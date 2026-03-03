import httpx

def fetch_leetcode_data(username: str) -> dict:
    """Fetch LeetCode stats via their public GraphQL API"""
    url = "https://leetcode.com/graphql"
    query = """
    query getUserProfile($username: String!) {
      matchedUser(username: $username) {
        username
        submitStats: submitStatsGlobal {
          acSubmissionNum {
            difficulty
            count
          }
        }
        profile {
          ranking
          reputation
          starRating
        }
        badges {
          id
          displayName
        }
      }
    }
    """
    try:
        resp = httpx.post(
            url,
            json={"query": query, "variables": {"username": username}},
            headers={"Content-Type": "application/json", "Referer": "https://leetcode.com"},
            timeout=10
        )
        data = resp.json()
        user = data.get("data", {}).get("matchedUser")
        if not user:
            return {"error": "User not found", "username": username}

        stats = user.get("submitStats", {}).get("acSubmissionNum", [])
        solved = {s["difficulty"]: s["count"] for s in stats}
        profile = user.get("profile", {})

        return {
            "username": username,
            "ranking": profile.get("ranking", 0),
            "total_solved": solved.get("All", 0),
            "easy_solved": solved.get("Easy", 0),
            "medium_solved": solved.get("Medium", 0),
            "hard_solved": solved.get("Hard", 0),
            "reputation": profile.get("reputation", 0),
            "badges": len(user.get("badges", [])),
        }
    except Exception as e:
        return {"error": str(e), "username": username}