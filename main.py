import asyncio
import os
import traceback

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

try:
    from github import fetch_github_data
    from analytics import calculate_skill_score
    from leetcode import fetch_leetcode_data
    from codeforces import fetch_codeforces_data
    from contributions import fetch_contributions
except ImportError as e:
    print(f"IMPORT ERROR: {e}")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def home():
    return {"status": "online", "message": "Developer Intelligence API"}

@app.get("/analyze/{username}")
async def analyze(username: str):
    try:
        repos = fetch_github_data(username)
        analytics = calculate_skill_score(repos)
        return {"username": username, "analytics": analytics, "repositories": repos}
    except Exception as e:
        print(traceback.format_exc())
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/leetcode/{username}")
async def leetcode(username: str):
    try:
        # Handle both sync and async versions
        import inspect
        if inspect.iscoroutinefunction(fetch_leetcode_data):
            data = await fetch_leetcode_data(username)
        else:
            data = fetch_leetcode_data(username)
        if isinstance(data, dict) and data.get("error"):
            return JSONResponse(status_code=404, content=data)
        return data
    except Exception as e:
        print(traceback.format_exc())
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/codeforces/{handle}")
async def codeforces(handle: str):
    try:
        import inspect
        if inspect.iscoroutinefunction(fetch_codeforces_data):
            data = await fetch_codeforces_data(handle)
        else:
            data = fetch_codeforces_data(handle)
        if isinstance(data, dict) and data.get("error"):
            return JSONResponse(status_code=404, content=data)
        return data
    except Exception as e:
        print(traceback.format_exc())
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/contributions/{username}")
async def contributions(username: str):
    try:
        import inspect
        from contributions import fetch_contributions
        data = await fetch_contributions(username)
        if data.get("error"):
            return JSONResponse(status_code=404, content=data)
        return data
    except Exception as e:
        print(traceback.format_exc())
        return JSONResponse(status_code=500, content={"error": str(e)})

# Auth endpoints
@app.get("/auth/me")
async def auth_me():
    # Placeholder: return user info if authenticated
    return {"user": "authenticated_user", "id": 1}

@app.post("/auth/oauth")
async def auth_oauth():
    # Placeholder: handle OAuth
    return {"status": "oauth_handled"}

@app.post("/auth/logout")
async def auth_logout():
    # Placeholder: handle logout
    return {"status": "logged_out"}

@app.get("/profile")
async def get_profile():
    # Placeholder: return user profile
    return {"username": "user", "email": "user@example.com", "bio": "Developer"}