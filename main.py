import os
from dotenv import load_dotenv
import pymongo
from pymongo import MongoClient
import requests
import jwt

# Load environment variables from .env file
load_dotenv()

from fastapi import FastAPI, Request, Response, HTTPException, Header, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, Dict, Any
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path

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
# frontend origin(s) explicitly. FRONTEND_ORIGINS can add more origins,
# but safe defaults are always included to prevent lockouts.
default_origins = [
    "http://localhost:3000",
    "https://deviq.online",
    "https://www.deviq.online",
    "https://saket21s.github.io",
    "https://developerintelligencedashboard.web.app",
]
front = os.environ.get("FRONTEND_ORIGINS", "")
extra_origins = [o.strip() for o in front.split(",") if o.strip()]
allow_list = sorted(set(default_origins + extra_origins))
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
    code: Optional[str] = None
    redirect_uri: Optional[str] = None
    name: Optional[str] = None
    email: Optional[str] = None
    avatar: Optional[str] = None
    profile_picture_url: Optional[str] = None
    provider: Optional[str] = "google"
    user: Optional[Dict[str, Any]] = None


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
        client = Groq(api_key=groq_api_key)
        
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
        
        # Call Groq API with llama-3-8b (fast, free model)
        completion = client.chat.completions.create(
            model="llama-3.1-8b-instant",  # Fast and available
            messages=messages,
            temperature=0.7,
            max_tokens=500,
        )
        
        result = completion.choices[0].message.content
        return {
            "result": result,
            "status": "success"
        }
    
    except Exception as e:
        print(f"Error calling Groq API: {e}")
        raise HTTPException(500, f"AI service error: {str(e)}")


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
    """Exchange OAuth code and return app token + normalized user profile."""
    provider = (data.provider or "google").lower()
    code = data.code
    redirect_uri = data.redirect_uri

    if provider not in {"google", "github"}:
        raise HTTPException(400, "Unsupported provider")
    if not code:
        raise HTTPException(400, "Missing authorization code")
    if not redirect_uri:
        raise HTTPException(400, "Missing redirect_uri")

    try:
        user_info = None

        if provider == "google":
            client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
            client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
            if not client_id or not client_secret:
                raise HTTPException(500, "Google OAuth is not configured")

            token_resp = requests.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
                timeout=15,
            )
            if token_resp.status_code != 200:
                raise HTTPException(400, "Google token exchange failed")

            google_access_token = token_resp.json().get("access_token")
            if not google_access_token:
                raise HTTPException(400, "Google access token missing")

            user_resp = requests.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {google_access_token}"},
                timeout=15,
            )
            if user_resp.status_code != 200:
                raise HTTPException(400, "Failed to fetch Google user profile")

            g = user_resp.json()
            user_info = {
                "id": g.get("id") or g.get("email"),
                "name": g.get("name") or (g.get("email") or "").split("@")[0],
                "email": g.get("email"),
                "profile_picture_url": g.get("picture"),
                "provider": "google",
            }

        else:
            client_id = os.environ.get("GITHUB_CLIENT_ID", "")
            client_secret = os.environ.get("GITHUB_CLIENT_SECRET", "")
            if not client_id or not client_secret:
                raise HTTPException(500, "GitHub OAuth is not configured")

            token_resp = requests.post(
                "https://github.com/login/oauth/access_token",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
                headers={"Accept": "application/json"},
                timeout=15,
            )
            if token_resp.status_code != 200:
                raise HTTPException(400, "GitHub token exchange failed")

            github_access_token = token_resp.json().get("access_token")
            if not github_access_token:
                raise HTTPException(400, "GitHub access token missing")

            user_resp = requests.get(
                "https://api.github.com/user",
                headers={
                    "Authorization": f"Bearer {github_access_token}",
                    "Accept": "application/vnd.github+json",
                },
                timeout=15,
            )
            if user_resp.status_code != 200:
                raise HTTPException(400, "Failed to fetch GitHub user profile")

            gh = user_resp.json()
            email = gh.get("email")
            if not email:
                emails_resp = requests.get(
                    "https://api.github.com/user/emails",
                    headers={
                        "Authorization": f"Bearer {github_access_token}",
                        "Accept": "application/vnd.github+json",
                    },
                    timeout=15,
                )
                if emails_resp.status_code == 200:
                    emails = emails_resp.json()
                    primary = next((e for e in emails if e.get("primary") and e.get("verified")), None)
                    fallback = next((e for e in emails if e.get("verified")), None)
                    chosen = primary or fallback
                    email = chosen.get("email") if chosen else None

            if not email:
                raise HTTPException(400, "GitHub account does not expose an email")

            user_info = {
                "id": str(gh.get("id") or email),
                "name": gh.get("name") or gh.get("login") or email.split("@")[0],
                "email": email,
                "profile_picture_url": gh.get("avatar_url"),
                "provider": "github",
            }

        if not user_info or not user_info.get("email"):
            raise HTTPException(400, "Provider did not return a usable email")

        # Best-effort user sync; auth should still work if DB is temporarily unavailable.
        if db is not None:
            try:
                users_collection = db["users"]
                payload = {
                    "name": user_info.get("name"),
                    "email": user_info.get("email"),
                    "avatar": user_info.get("profile_picture_url"),
                    "provider": user_info.get("provider"),
                    "updatedAt": datetime.now(timezone.utc).isoformat(),
                }
                existing_user = users_collection.find_one({"email": user_info.get("email")})
                if not existing_user:
                    payload["createdAt"] = datetime.now(timezone.utc).isoformat()
                users_collection.update_one({"email": user_info.get("email")}, {"$set": payload}, upsert=True)
            except Exception as db_err:
                # Do not block OAuth login on database issues.
                print(f"⚠️ OAuth DB sync skipped: {db_err}")

        jwt_secret = os.environ.get("JWT_SECRET", "deviq_dev_secret_change_me")
        app_token = jwt.encode(
            {
                "sub": user_info.get("email"),
                "email": user_info.get("email"),
                "provider": user_info.get("provider"),
                "exp": datetime.now(timezone.utc) + timedelta(days=7),
            },
            jwt_secret,
            algorithm="HS256",
        )

        return {
            "access_token": app_token,
            "token_type": "bearer",
            "provider": user_info.get("provider"),
            "user": user_info,
        }

    except HTTPException:
        raise
    except requests.RequestException as e:
        raise HTTPException(502, f"OAuth provider request failed: {str(e)}")
    except Exception as e:
        raise HTTPException(500, f"OAuth sync failed: {str(e)}")


@app.get("/analyze/{username}")
def analyze(username: str):
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

@app.get("/codeforces/{username}")
def codeforces_analyze(username: str):
    """Fetch Codeforces profile data for a user"""
    # Placeholder: return mock Codeforces data
    # In production, this would fetch from Codeforces API
    return {
        "username": username,
        "rating": 1400,
        "max_rating": 1500,
        "rank": "Expert",
        "max_rank": "Expert",
        "problems_solved": 250,
        "contest_count": 20
    }


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
        raise HTTPException(500, "Firebase not configured")

    uid = await resolve_uid(authorization, x_user_email)
    user_doc = db.collection("users").document(uid).get()
    if not user_doc.exists:
        return {"accounts": []}

    user_data = user_doc.to_dict() or {}
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
        raise HTTPException(500, "Firebase not configured")

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
    user_doc = db.collection("users").document(uid).get()
    current_accounts = {}
    if user_doc.exists:
        current_accounts = user_doc.to_dict().get("connected_accounts", {}) or {}
    
    # Update the specific platform account
    current_accounts[normalized_platform] = {
        "platform": normalized_platform,
        "platform_username": username,
        "is_active": True,
        "connected_at": now,
        "last_synced_at": now,
    }
    
    # Save properly nested structure
    db.collection("users").document(uid).set({
        "connected_accounts": current_accounts,
        "updatedAt": firestore.SERVER_TIMESTAMP
    }, merge=True)

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
        raise HTTPException(500, "Firebase not configured")

    normalized_platform = platform.strip().lower()
    if normalized_platform not in {"github", "leetcode", "codeforces"}:
        raise HTTPException(400, "Unsupported platform")

    uid = await resolve_uid(authorization, x_user_email)
    now = _iso_now()
    
    # Get current user document
    user_doc = db.collection("users").document(uid).get()
    current_accounts = {}
    if user_doc.exists:
        current_accounts = user_doc.to_dict().get("connected_accounts", {}) or {}
    
    # Mark the platform account as inactive
    if normalized_platform in current_accounts:
        current_accounts[normalized_platform]["is_active"] = False
        current_accounts[normalized_platform]["last_synced_at"] = now
    
    # Save properly nested structure
    db.collection("users").document(uid).set({
        "connected_accounts": current_accounts,
        "updatedAt": firestore.SERVER_TIMESTAMP
    }, merge=True)

    return {"ok": True, "platform": normalized_platform}

