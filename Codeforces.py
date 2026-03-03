import httpx
import asyncio

async def fetch_codeforces_data(handle: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            u_res, s_res, r_res = await asyncio.gather(
                client.get(f"https://codeforces.com/api/user.info?handles={handle}"),
                client.get(f"https://codeforces.com/api/user.status?handle={handle}&from=1&count=2000"),
                client.get(f"https://codeforces.com/api/user.rating?handle={handle}"),
            )

        u_data = u_res.json()
        if u_data.get("status") != "OK":
            return {"error": u_data.get("comment", "User not found")}

        user = u_data["result"][0]

        s_data = s_res.json()
        subs = s_data.get("result", []) if s_data.get("status") == "OK" else []
        accepted = [x for x in subs if x.get("verdict") == "OK"]
        unique = len(set(
            f"{x['problem']['contestId']}-{x['problem']['index']}"
            for x in accepted
        ))

        r_data = r_res.json()
        contests = len(r_data.get("result", [])) if r_data.get("status") == "OK" else 0

        return {
            "username": user["handle"],
            "rating": user.get("rating", 0),
            "max_rating": user.get("maxRating", 0),
            "rank": user.get("rank", "unrated"),
            "max_rank": user.get("maxRank", "unrated"),
            "problems_solved": unique,
            "contests_participated": contests,
            "contribution": user.get("contribution", 0),
        }
    except Exception as e:
        return {"error": str(e)}