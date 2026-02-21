from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from github import fetch_github_data
from analytics import calculate_skill_score

app = FastAPI()

# ✅ Allowed origins (VERY IMPORTANT for Android + Web)
origins = [
    "http://localhost:3000",
    "http://localhost",
    "https://localhost",
    "http://127.0.0.1:3000",
    "https://developer-portfolio-backend-lhv5.onrender.com",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,   # required for proper browser handling
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------------
# Routes
# -------------------------------

@app.get("/")
def home():
    return {
        "message": "Developer Portfolio Intelligence API Running"
    }


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

    except Exception as e:
        return {
            "error": str(e)
        }