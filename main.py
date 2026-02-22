from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import os

# Ensure these files (github.py, analytics.py) are in your Render repository
from github import fetch_github_data
from analytics import calculate_skill_score

app = FastAPI()

# ✅ FINAL CORS CONFIGURATION
# This allows your Vercel frontend and mobile browsers to talk to this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins including your Vercel URL
    allow_credentials=True,
    allow_methods=["*"],  # Allows GET, POST, OPTIONS, etc.
    allow_headers=["*"],  # Allows all headers (Content-Type, etc.)
)

@app.get("/")
def home():
    return {"message": "Developer Portfolio Intelligence API Running"}

@app.get("/analyze/{username}")
def analyze(username: str):
    try:
        # 1. Fetch data from GitHub
        repos = fetch_github_data(username)
        
        # 2. Check if user exists or has repos
        if not repos and repos != []:
            return {"status": "error", "message": "User not found or API error"}

        # 3. Calculate analytics
        analytics = calculate_skill_score(repos)

        # 4. Return data in the exact format your page.tsx expects
        # Note: We return 'analytics' and 'repositories' directly to match your state
        return {
            "username": username,
            "analytics": analytics,
            "repositories": repos
        }
    except Exception as e:
        print(f"Error analyzing {username}: {e}")
        return {"status": "error", "message": str(e)}

# Note: Render handles the port automatically, 
# but locally you'd run: uvicorn main:app --host 0.0.0.0 --port 8000