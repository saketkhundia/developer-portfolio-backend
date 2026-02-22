from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import traceback

# Import your logic
try:
    from github import fetch_github_data
    from analytics import calculate_skill_score
except ImportError as e:
    print(f"IMPORT ERROR: {e}")

app = FastAPI()

# Standard Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",  # local development
        "https://developerintelligence.vercel.app",  # production frontend
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ✅ This catch-all prevents the "No Access-Control-Allow-Origin" error
@app.middleware("http")
async def add_cors_header(request: Request, call_next):
    response = await call_next(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "*"
    return response

@app.get("/")
def home():
    return {"status": "online", "message": "API is Live"}

@app.get("/analyze/{username}")
async def analyze(username: str):
    print(f"--- Analyzing User: {username} ---")
    try:
        repos = fetch_github_data(username)
        print(f"Fetched {len(repos)} repos")
        
        analytics = calculate_skill_score(repos)
        print("Analytics calculated successfully")

        return {
            "username": username,
            "analytics": analytics,
            "repositories": repos
        }
    except Exception as e:
        print(f"CRASH ERROR: {str(e)}")
        print(traceback.format_exc())
        return JSONResponse(
            status_code=500,
            content={"error": "Internal Server Error", "details": str(e)}
        )