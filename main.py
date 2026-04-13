import os
from dotenv import load_dotenv
from groq import Groq
import pymongo
from pymongo import MongoClient

# Load environment variables from .env file
load_dotenv()

from fastapi import FastAPI, Request, Response, HTTPException, Header, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, Dict, Any
from datetime import datetime, timezone
import json
from pathlib import Path
import requests

# Initialize MongoDB
MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")
try:
    mongo_client = MongoClient(MONGODB_URI)
    db = mongo_client["deviq"]  # Database name
    print(f"✅ Connected to MongoDB: {MONGODB_URI.split('@')[1] if '@' in MONGODB_URI else 'local'}")
except Exception as e:
    print(f"⚠️  MongoDB connection failed: {e}")
    print("See MONGODB_SETUP.md for instructions.")
    db = None

from github import fetch_github_data
from analytics import calculate_skill_score

app = FastAPI()

# allow_origins cannot be '*' when credentials=True; specify the
# frontend origin(s) explicitly. You can set FRONTEND_ORIGINS to a
# comma-separated list of allowed origins (e.g. http://localhost:3000).
front = os.environ.get("FRONTEND_ORIGINS", "http://localhost:3000,https://deviq.online,https://developerintelligencedashboard.web.app")
allow_list = [o.strip() for o in front.split(",") if o.strip()]
print(f"✅ CORS allowed origins: {allow_list}")

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Helper to verify Firebase token
async def verify_firebase_token(authorization: Optional[str] = Header(None)) -> str:
    """Verify Firebase ID token and return user ID (email-based for MongoDB)"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing authorization header")
    
    token = authorization.replace("Bearer ", "")
    # For now, use token as user ID (in production, verify with Firebase Auth)
    # Firebase SDK is still used for auth, just storing in MongoDB
    return token


@app.get("/")
def home():
    return {"message": "Developer Portfolio Intelligence API Running"}


class OAuthUserData(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    avatar: Optional[str] = None
    profile_picture_url: Optional[str] = None
    provider: Optional[str] = "google"
    code: Optional[str] = None
    redirect_uri: Optional[str] = None
    user: Optional[Dict[str, Any]] = None


def _exchange_google_code(code: str, redirect_uri: str) -> Dict[str, Any]:
    client_id = os.environ.get("GOOGLE_CLIENT_ID") or os.environ.get("NEXT_PUBLIC_GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise HTTPException(500, "Google OAuth is not configured")

    token_resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        timeout=15,
    )
    if token_resp.status_code >= 400:
        raise HTTPException(400, f"Google token exchange failed: {token_resp.text[:180]}")

    access_token = token_resp.json().get("access_token")
    if not access_token:
        raise HTTPException(400, "Google token exchange returned no access token")

    profile_resp = requests.get(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    if profile_resp.status_code >= 400:
        raise HTTPException(400, f"Google user fetch failed: {profile_resp.text[:180]}")

    profile = profile_resp.json()
    return {
        "name": profile.get("name") or profile.get("given_name") or "User",
        "email": profile.get("email"),
        "avatar": profile.get("picture"),
    }


def _exchange_github_code(code: str, redirect_uri: str) -> Dict[str, Any]:
    client_id = os.environ.get("GITHUB_CLIENT_ID") or os.environ.get("NEXT_PUBLIC_GITHUB_CLIENT_ID")
    client_secret = os.environ.get("GITHUB_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise HTTPException(500, "GitHub OAuth is not configured")

    token_resp = requests.post(
        "https://github.com/login/oauth/access_token",
        headers={"Accept": "application/json"},
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        },
        timeout=15,
    )
    if token_resp.status_code >= 400:
        raise HTTPException(400, f"GitHub token exchange failed: {token_resp.text[:180]}")

    access_token = token_resp.json().get("access_token")
    if not access_token:
        raise HTTPException(400, "GitHub token exchange returned no access token")

    user_resp = requests.get(
        "https://api.github.com/user",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "DevIQ-Backend",
        },
        timeout=15,
    )
    if user_resp.status_code >= 400:
        raise HTTPException(400, f"GitHub user fetch failed: {user_resp.text[:180]}")

    user_data = user_resp.json()
    email = user_data.get("email")
    if not email:
        email_resp = requests.get(
            "https://api.github.com/user/emails",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "DevIQ-Backend",
            },
            timeout=15,
        )
        if email_resp.status_code < 400:
            emails = email_resp.json() or []
            primary = next((e for e in emails if e.get("primary")), None)
            fallback = next((e for e in emails if e.get("verified")), None)
            picked = primary or fallback or (emails[0] if emails else None)
            email = picked.get("email") if isinstance(picked, dict) else None

    return {
        "name": user_data.get("name") or user_data.get("login") or "GitHub User",
        "email": email,
        "avatar": user_data.get("avatar_url"),
    }


class ConnectAccountBody(BaseModel):
    username: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class ChatRequest(BaseModel):
    prompt: str
    conversation_history: Optional[list] = None


@app.post("/ai/insights")
async def ai_insights(
    body: ChatRequest = Body(...),
    authorization: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
):
    """Generate AI insights using Groq API"""
    groq_api_key = os.environ.get("GROQ_API_KEY", "")
    if not groq_api_key:
        raise HTTPException(500, "Groq API key not configured")
    
    try:
        # Initialize Groq client with only the API key
        client = Groq(
            api_key=groq_api_key,
        )
        
        # Prepare messages for Groq API
        messages = [
            {
                "role": "system",
                "content": "You are a helpful AI assistant for developers. You help them understand their coding profiles, analyze their skills, and provide insights about their progress. Be concise, encouraging, and practical in your responses."
            },
            {
                "role": "user",
                "content": body.prompt
            }
        ]
        
        # Call Groq API with llama-3.1-8b (fast, available model)
        completion = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=messages,
            temperature=0.7,
            max_tokens=500,
        )
        
        result = completion.choices[0].message.content
        return {
            "result": result,
            "status": "success"
        }
    
    except HTTPException:
        raise
    except Exception as e:
        error_msg = str(e)
        print(f"Groq API Error: {error_msg}")
        import traceback
        print(traceback.format_exc())
        raise HTTPException(500, f"AI service error: {error_msg}")


def _uid_from_email(email: str) -> str:
    return email.strip().lower().replace("@", "_").replace(".", "_")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def resolve_uid(
    authorization: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
) -> str:
    """
    Resolve user identity from Firebase token when available.
    Falls back to x-user-email for local/dev flows where no token is stored.
    """
    if authorization and authorization.startswith("Bearer "):
        return await verify_firebase_token(authorization)

    if x_user_email and x_user_email.strip():
        return _uid_from_email(x_user_email)

    raise HTTPException(401, "Missing authorization")


@app.post("/auth/oauth")
async def oauth_login(data: OAuthUserData):
    """Register/login an OAuth user (Google or GitHub)"""
    if db is None:
        raise HTTPException(500, "MongoDB not configured")
    
    # Extract user data from request - handle both flat and nested structures
    name = data.name
    email = data.email
    avatar = data.avatar or data.profile_picture_url
    provider = data.provider or "google"
    
    # Check nested user object if top-level fields are missing
    if data.user and isinstance(data.user, dict):
        name = name or data.user.get("name")
        email = email or data.user.get("email")
        avatar = avatar or data.user.get("picture")

    # OAuth code-based flow (used by frontend callback route)
    if data.code:
        redirect_uri = (data.redirect_uri or "").strip()
        if not redirect_uri:
            raise HTTPException(400, "redirect_uri is required for OAuth code exchange")

        try:
            if provider == "google":
                exchanged = _exchange_google_code(data.code, redirect_uri)
            elif provider == "github":
                exchanged = _exchange_github_code(data.code, redirect_uri)
            else:
                raise HTTPException(400, f"Unsupported OAuth provider: {provider}")
        except Exception as e:
            # Detailed logging for OAuth exchange failures
            print(f"CRITICAL: OAuth code exchange failed for provider '{provider}'.")
            print(f"  - Code: {data.code[:10]}... (truncated)")
            print(f"  - Redirect URI: {redirect_uri}")
            print(f"  - Error: {e}")
            import traceback
            print(traceback.format_exc())
            # Re-raise to send error to client
            raise HTTPException(status_code=500, detail=f"Failed to exchange OAuth code: {str(e)}")

        name = exchanged.get("name") or name
        email = exchanged.get("email") or email
        avatar = exchanged.get("avatar") or avatar
    
    print("OAuth request received")
    print(f"Parsed payload: name={name}, email={email}, provider={provider}")
    
    if not email:
        print("OAuth request missing email")
        raise HTTPException(400, "Email is required")

    # Fallback name so OAuth can still succeed when providers omit display name.
    if not name:
        name = email.split("@")[0]
    
    try:
        # Use email as MongoDB document ID
        users_collection = db["users"]
        
        payload = {
            "name": name,
            "email": email,
            "avatar": avatar,
            "provider": provider,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        }
        
        # Check if user exists
        existing_user = users_collection.find_one({"email": email})
        if not existing_user:
            payload["createdAt"] = datetime.now(timezone.utc).isoformat()
        
        # Upsert user
        users_collection.update_one(
            {"email": email},
            {"$set": payload},
            upsert=True
        )
        
        print(f"OAuth profile synced for {email}")

        # Retrieve and return user data
        user_doc = users_collection.find_one({"email": email})

        if not user_doc:
            print("OAuth profile write verification failed")
            raise HTTPException(500, "Could not verify user was created")

        # Remove MongoDB ID from response for cleaner JSON
        user_data = dict(user_doc)
        user_data.pop("_id", None)
        print("OAuth endpoint completed successfully")
        
        return {
            "user": user_data,
            "uid": email,
            "access_token": email,
            "message": "OAuth user synced successfully"
        }
    
    except HTTPException:
        raise
    except Exception as e:
        print("Unexpected error in OAuth login")
        print(f"Error type: {type(e).__name__}")
        print(f"Error message: {str(e)}")
        import traceback
        print(traceback.format_exc())
        raise HTTPException(500, f"OAuth sync failed: {str(e)}")


@app.get("/contributions/{username}")
def get_contributions(username: str):
    """Fetch GitHub contribution calendar data for a user"""
    github_token = os.environ.get("GITHUB_TOKEN", "")
    if not github_token:
        raise HTTPException(500, "GitHub token not configured")
    
    try:
        # GraphQL query for contribution data
        query = """
        query($userName:String!) {
          user(login: $userName) {
            contributionsCollection {
              contributionCalendar {
                totalContributions
                weeks {
                  contributionDays {
                    contributionCount
                    date
                    contributionLevel
                  }
                }
              }
            }
          }
        }
        """
        
        variables = {"userName": username}
        
        response = requests.post(
            "https://api.github.com/graphql",
            json={"query": query, "variables": variables},
            headers={
                "Authorization": f"Bearer {github_token}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        
        if response.status_code != 200:
            raise HTTPException(400, f"GitHub API error: {response.status_code}")
        
        data = response.json()
        
        if "errors" in data:
            raise HTTPException(400, f"GitHub GraphQL error: {data['errors']}")
        
        if not data.get("data") or not data["data"].get("user"):
            raise HTTPException(404, f"GitHub user not found: {username}")
        
        calendar = data["data"]["user"]["contributionsCollection"]["contributionCalendar"]
        
        # Transform contribution_calendar into expected format
        contributions_raw = []
        
        for week in calendar.get("weeks", []):
            for day in week.get("contributionDays", []):
                contributions_raw.append({
                    "date": day["date"],
                    "count": day["contributionCount"]
                })
        
        # Calculate level based on count quartiles (more reliable than GitHub's API response)
        if not contributions_raw:
            contributions = []
        else:
            counts = sorted([c["count"] for c in contributions_raw if c["count"] > 0])
            if not counts:
                contributions = [{"date": c["date"], "count": 0, "level": 0} for c in contributions_raw]
            else:
                q1 = counts[len(counts) // 4]
                q2 = counts[len(counts) // 2]
                q3 = counts[3 * len(counts) // 4]
                
                contributions = []
                for c in contributions_raw:
                    if c["count"] == 0:
                        level = 0
                    elif c["count"] <= q1:
                        level = 1
                    elif c["count"] <= q2:
                        level = 2
                    elif c["count"] <= q3:
                        level = 3
                    else:
                        level = 4
                    contributions.append({
                        "date": c["date"],
                        "count": c["count"],
                        "level": level
                    })
        
        # Calculate streaks and busiest day
        current_streak = 0
        longest_streak = 0
        streak = 0
        total_contrib = 0
        busiest_day = None
        max_count = 0
        
        # Process in reverse order for current streak (most recent first)
        for day_data in reversed(contributions):
            if day_data["count"] > 0:
                current_streak += 1
            else:
                break
        
        # Calculate longest streak
        for day_data in contributions:
            total_contrib += day_data["count"]
            if day_data["count"] > 0:
                streak += 1
                longest_streak = max(longest_streak, streak)
            else:
                streak = 0
            
            if day_data["count"] > max_count:
                max_count = day_data["count"]
                busiest_day = {"date": day_data["date"], "count": day_data["count"]}
        
        return {
            "contributions": contributions,
            "total_last_year": calendar.get("totalContributions", total_contrib),
            "current_streak": current_streak,
            "longest_streak": longest_streak,
            "busiest_day": busiest_day
        }
    
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error fetching GitHub contributions for {username}: {e}")
        raise HTTPException(500, f"Failed to fetch contributions: {str(e)}")


@app.get("/analyze/{username}")
def analyze(username: str):
    """Fetch and analyze GitHub repositories for a user"""
    try:
        repos = fetch_github_data(username)
        analytics = calculate_skill_score(repos)
        return {
            "username": username,
            "analytics": analytics,
            "repositories": repos
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch GitHub data: {str(e)}")


@app.get("/leetcode/{username}")
def leetcode_analyze(username: str):
    """Fetch LeetCode profile data for a user"""
    # Placeholder: return mock LeetCode data
    # In production, this would fetch from LeetCode API
    return {
        "username": username,
        "total_solved": 150,
        "easy_solved": 80,
        "medium_solved": 50,
        "hard_solved": 20,
        "acceptance_rate": 65.5,
        "ranking": 25000,
        "reputation": 45
    }


def fetch_leetcode_company_problems(slug: str) -> dict:
    """Fetch real company problems from LeetCode GraphQL API"""
    try:
        graphql_query = """
        query getCompanyProblems($slug: String!, $limit: Int!) {
            allQuestionsCount {
                difficulty
                total
            }
            companyProblems(companySlug: $slug, limit: $limit, skip: 0) {
                problems {
                    frontendQuestionId
                    title
                    titleSlug
                    difficulty
                    topicTags {
                        slug
                        name
                    }
                    isPaidOnly
                    frequency
                }
                total
            }
            companyInfo {
                name
                slug
            }
        }
        """
        response = requests.post(
            "https://leetcode.com/graphql",
            json={"query": graphql_query, "variables": {"slug": slug, "limit": 200}},
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            },
            timeout=10
        )
        
        if response.status_code == 200:
            data = response.json()
            if "data" in data and data["data"].get("companyProblems"):
                company_info = data["data"].get("companyInfo", {})
                problems_data = data["data"]["companyProblems"]
                problems = problems_data.get("problems", [])
                
                # Transform to match expected format
                transformed_problems = []
                for p in problems:
                    transformed_problems.append({
                        "id": p.get("frontendQuestionId", ""),
                        "title": p.get("title", ""),
                        "slug": p.get("titleSlug", ""),
                        "difficulty": p.get("difficulty", "Medium"),
                        "topicTags": [tag.get("name", "") for tag in p.get("topicTags", [])],
                        "paidOnly": p.get("isPaidOnly", False),
                        "frequency": p.get("frequency", 0),
                        "url": f"https://leetcode.com/problems/{p.get('titleSlug', '')}/"
                    })
                
                return {
                    "company": company_info.get("name") or slug.capitalize(),
                    "slug": slug,
                    "total_problems": problems_data.get("total", len(transformed_problems)),
                    "last_updated": "2026-04-13",
                    "problems": transformed_problems[:50]  # Return top 50 problems
                }
    except Exception as e:
        print(f"Error fetching from LeetCode API for {slug}: {e}")
    
    return None


@app.get("/leetcode/company-problems/{slug}")
def get_company_problems(slug: str):
    """Fetch real LeetCode problems for a specific company"""
    slug_lower = slug.lower()
    
    # Try to fetch real data from LeetCode API
    company_problems = fetch_leetcode_company_problems(slug_lower)
    
    if company_problems:
        return company_problems
    
    # If API fails, return error
    raise HTTPException(404, f"Company '{slug}' not found or could not fetch data")


@app.get("/codeforces/{username}")
def codeforces_analyze(username: str):
    """Fetch real Codeforces profile data for a user."""
    try:
        # 1) User profile and rating/rank info
        info_resp = requests.get(
            "https://codeforces.com/api/user.info",
            params={"handles": username},
            timeout=15,
        )
        if info_resp.status_code != 200:
            raise HTTPException(400, f"Codeforces API error: {info_resp.status_code}")

        info_data = info_resp.json()
        if info_data.get("status") != "OK" or not info_data.get("result"):
            raise HTTPException(404, f"Codeforces user not found: {username}")

        user = info_data["result"][0]

        # 2) Contest history (used for contests participated)
        rating_resp = requests.get(
            "https://codeforces.com/api/user.rating",
            params={"handle": username},
            timeout=15,
        )
        contests_participated = 0
        if rating_resp.status_code == 200:
            rating_data = rating_resp.json()
            if rating_data.get("status") == "OK" and isinstance(rating_data.get("result"), list):
                contests_participated = len(rating_data["result"])

        # 3) Approximate solved problems from accepted submissions
        # Count unique accepted problems by contestId + index
        solved_count = 0
        status_resp = requests.get(
            "https://codeforces.com/api/user.status",
            params={"handle": username, "from": 1, "count": 10000},
            timeout=20,
        )
        if status_resp.status_code == 200:
            status_data = status_resp.json()
            if status_data.get("status") == "OK" and isinstance(status_data.get("result"), list):
                solved = set()
                for sub in status_data["result"]:
                    if sub.get("verdict") != "OK":
                        continue
                    problem = sub.get("problem") or {}
                    cid = problem.get("contestId")
                    idx = problem.get("index")
                    if cid is not None and idx:
                        solved.add(f"{cid}-{idx}")
                solved_count = len(solved)

        return {
            "username": user.get("handle", username),
            "rating": user.get("rating", 0),
            "max_rating": user.get("maxRating", 0),
            "rank": user.get("rank", "unrated"),
            "max_rank": user.get("maxRank", "unrated"),
            "problems_solved": solved_count,
            "contests_participated": contests_participated,
            "contribution": user.get("contribution", 0),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Failed to fetch Codeforces data: {str(e)}")


# ─────────────────────────────────────────────────
# Firebase Authentication endpoints
# ─────────────────────────────────────────────────

@app.get("/auth/me")
async def me(authorization: Optional[str] = Header(None)):
    """Get current authenticated user profile"""
    if db is None:
        raise HTTPException(500, "MongoDB not configured")
    
    uid = await verify_firebase_token(authorization)
    users_collection = db["users"]
    user_doc = users_collection.find_one({"email": uid})
    
    if not user_doc:
        raise HTTPException(404, "User profile not found")
    
    user_doc.pop("_id", None)
    return user_doc

@app.post("/auth/logout")
def logout():
    """Logout (client-side token deletion)"""
    return {"ok": True}

@app.delete("/auth/account")
async def delete_account(authorization: Optional[str] = Header(None)):
    """Delete authenticated user account"""
    if db is None:
        raise HTTPException(500, "MongoDB not configured")
    
    uid = await verify_firebase_token(authorization)
    
    # Delete user profile from MongoDB
    users_collection = db["users"]
    users_collection.delete_one({"email": uid})
    
    return {"ok": True}

@app.get("/profile")
async def get_profile(
    authorization: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
):
    """Get user's portfolio profile data"""
    if db is None:
        raise HTTPException(500, "MongoDB not configured")
    
    uid = await resolve_uid(authorization, x_user_email)
    users_collection = db["users"]
    user_doc = users_collection.find_one({"email": uid})
    
    if not user_doc:
        return {"profile": {}}
    
    return user_doc.get("profile", {})

@app.put("/profile")
async def set_profile(
    data: dict,
    authorization: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
):
    """Update user's portfolio profile data"""
    if db is None:
        raise HTTPException(500, "MongoDB not configured")
    
    uid = await resolve_uid(authorization, x_user_email)
    
    # Update profile in MongoDB
    users_collection = db["users"]
    users_collection.update_one(
        {"email": uid},
        {
            "$set": {
                "profile": data,
                "updatedAt": datetime.now(timezone.utc).isoformat()
            }
        },
        upsert=True
    )
    
    return data

@app.post("/sync/profile")
async def sync_profile(
    data: dict = Body(...),
    authorization: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
):
    """Sync user profile data to backend (supports token or email fallback)"""
    if db is None:
        raise HTTPException(500, "MongoDB not configured")
    
    uid = await resolve_uid(authorization, x_user_email)
    
    # Update profile in MongoDB with merge to preserve existing data
    users_collection = db["users"]
    users_collection.update_one(
        {"email": uid},
        {
            "$set": {
                "profile": data,
                "updatedAt": datetime.now(timezone.utc).isoformat()
            }
        },
        upsert=True
    )
    
    # Return updated profile (same data that was sent)
    return {
        "message": "Profile synced successfully",
        "user": data
    }

@app.post("/profile/picture")
async def save_profile_picture(
    body: Dict[str, Any] = Body(...),
    authorization: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
):
    """Save user's profile picture URL"""
    if db is None:
        raise HTTPException(500, "MongoDB not configured")
    
    uid = await resolve_uid(authorization, x_user_email)
    picture_url = body.get("picture_url", "")
    
    # Update profile picture in MongoDB with merge to preserve existing data
    users_collection = db["users"]
    users_collection.update_one(
        {"email": uid},
        {
            "$set": {
                "profile.profile_picture_url": picture_url,
                "profile.avatar": picture_url,
                "updatedAt": datetime.now(timezone.utc).isoformat()
            }
        },
        upsert=True
    )
    
    return {
        "message": "Profile picture saved",
        "picture_url": picture_url
    }


# ─────────────────────────────────────────────────
# Connected Accounts endpoints
# ─────────────────────────────────────────────────

@app.get("/accounts/connected")
async def get_connected_accounts(
    authorization: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
):
    if db is None:
        raise HTTPException(500, "MongoDB not configured")

    uid = await resolve_uid(authorization, x_user_email)
    users_collection = db["users"]
    user_doc = users_collection.find_one({"email": uid})
    if not user_doc:
        return {"accounts": []}

    user_data = user_doc or {}
    connected_map = user_data.get("connected_accounts", {})
    if not isinstance(connected_map, dict):
        return {"accounts": []}

    accounts = []
    for platform, value in connected_map.items():
        if isinstance(value, dict):
            accounts.append({
                "platform": value.get("platform", platform),
                "platform_username": value.get("platform_username", ""),
                "is_active": bool(value.get("is_active", False)),
                "connected_at": value.get("connected_at", ""),
                "last_synced_at": value.get("last_synced_at", ""),
            })

    return {"accounts": accounts}


@app.post("/accounts/connect/{platform}")
async def connect_account(
    platform: str,
    body: ConnectAccountBody,
    authorization: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
):
    if db is None:
        raise HTTPException(500, "MongoDB not configured")

    normalized_platform = platform.strip().lower()
    if normalized_platform not in {"github", "leetcode", "codeforces"}:
        raise HTTPException(400, "Unsupported platform")

    uid = await resolve_uid(authorization, x_user_email)

    username = (body.username or "").strip()
    if not username and isinstance(body.metadata, dict):
        username = (
            str(body.metadata.get("username") or "").strip()
            or str(body.metadata.get("login") or "").strip()
            or str(body.metadata.get("name") or "").strip()
        )

    if not username:
        raise HTTPException(400, "Username is required")

    now = _iso_now()
    
    # Get current user document
    users_collection = db["users"]
    user_doc = users_collection.find_one({"email": uid})
    current_accounts = {}
    if user_doc:
        current_accounts = user_doc.get("connected_accounts", {}) or {}
    
    # Update the specific platform account
    current_accounts[normalized_platform] = {
        "platform": normalized_platform,
        "platform_username": username,
        "is_active": True,
        "connected_at": now,
        "last_synced_at": now,
    }
    
    # Save properly nested structure
    users_collection.update_one(
        {"email": uid},
        {
            "$set": {
                "connected_accounts": current_accounts,
                "updatedAt": datetime.now(timezone.utc).isoformat()
            }
        },
        upsert=True
    )

    return {
        "ok": True,
        "platform": normalized_platform,
        "platform_username": username,
    }


@app.delete("/accounts/disconnect/{platform}")
async def disconnect_account(
    platform: str,
    authorization: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
):
    if db is None:
        raise HTTPException(500, "MongoDB not configured")

    normalized_platform = platform.strip().lower()
    if normalized_platform not in {"github", "leetcode", "codeforces"}:
        raise HTTPException(400, "Unsupported platform")

    uid = await resolve_uid(authorization, x_user_email)
    now = _iso_now()
    
    # Get current user document
    users_collection = db["users"]
    user_doc = users_collection.find_one({"email": uid})
    current_accounts = {}
    if user_doc:
        current_accounts = user_doc.get("connected_accounts", {}) or {}
    
    # Mark the platform account as inactive
    if normalized_platform in current_accounts:
        current_accounts[normalized_platform]["is_active"] = False
        current_accounts[normalized_platform]["last_synced_at"] = now
    
    # Save properly nested structure
    users_collection.update_one(
        {"email": uid},
        {
            "$set": {
                "connected_accounts": current_accounts,
                "updatedAt": datetime.now(timezone.utc).isoformat()
            }
        },
        upsert=True
    )

    return {"ok": True, "platform": normalized_platform}


class LogEntry(BaseModel):
    level: str = "error"
    message: str
    context: Optional[Dict[str, Any]] = None


@app.post("/log")
async def receive_log(entry: LogEntry):
    """Receives a client-side log entry and prints it to the server console."""
    timestamp = datetime.now(timezone.utc).isoformat()
    print(f"CLIENT LOG [{entry.level.upper()}] @ {timestamp}: {entry.message}")
    if entry.context:
        # Pretty-print context for readability
        context_str = json.dumps(entry.context, indent=2)
        print(f"  Context: {context_str}")
    return {"status": "logged"}

