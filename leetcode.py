def fetch_company_tagged_questions(company_slug: str) -> dict:
  """Fetch LeetCode problems tagged with a specific company via GraphQL API"""
  url = "https://leetcode.com/graphql"
  query = """
  query getCompanyTag($slug: String!) {
    companyTag(slug: $slug) {
    name
    slug
    questions {
      title
      titleSlug
      difficulty
      topicTags { name slug }
      stats
      paidOnly
      questionFrontendId
      freqBar
    }
    }
  }
  """
  slug = company_slug.lower().strip().replace(" ", "-")
  try:
    resp = httpx.post(
      url,
      json={"query": query, "variables": {"slug": slug}},
      headers={"Content-Type": "application/json", "Referer": "https://leetcode.com"},
      timeout=15
    )
    raw = resp.json()
    tag = (raw.get("data") or {}).get("companyTag")
    if not tag or not tag.get("questions"):
      return {"error": f"No problems found for company '{company_slug}'", "company": company_slug}
    problems = []
    for q in tag["questions"]:
      problems.append({
        "id": q.get("questionFrontendId", ""),
        "title": q.get("title", ""),
        "slug": q.get("titleSlug", ""),
        "difficulty": q.get("difficulty", "Medium"),
        "topicTags": [t["name"] for t in (q.get("topicTags") or [])],
        "paidOnly": q.get("paidOnly", False),
        "frequency": q.get("freqBar") or 0,
        "url": f"https://leetcode.com/problems/{q.get('titleSlug', '')}/",
      })
    problems.sort(key=lambda x: x.get("frequency", 0), reverse=True)
    return {
      "company": tag.get("name", company_slug),
      "slug": slug,
      "total_problems": len(problems),
      "problems": problems,
      "last_updated": datetime.utcnow().isoformat(),
    }
  except Exception as e:
    return {"error": str(e), "company": company_slug}
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