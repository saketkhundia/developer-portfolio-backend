from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from github import fetch_github_data
from analytics import calculate_skill_score

app = FastAPI()

# ✅ Proper CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],  # frontend origin
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def home():
    return {"message": "Developer Portfolio Intelligence API Running"}

@app.get("/analyze/{username}")
def analyze(username: str):
    repos = fetch_github_data(username)
    analytics = calculate_skill_score(repos)

    return {
        "username": username,
        "analytics": analytics,
        "repositories": repos
    }
