from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import traceback

# Import your logic
from github import fetch_github_data
from analytics import calculate_skill_score

app = FastAPI()

# ✅ FORCE CORS FOR EVERYTHING
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ✅ Catch-all error handler to prevent "No Access-Control-Allow-Origin"
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"status": "error", "message": str(exc), "trace": traceback.format_exc()},
        headers={"Access-Control-Allow-Origin": "*"} # Manually inject header
    )

@app.get("/")
def home():
    return {"message": "API is Live"}

@app.get("/analyze/{username}")
def analyze(username: str):
    # Log the request in Render logs so you can see it working
    print(f"Request received for user: {username}")
    
    repos = fetch_github_data(username)
    analytics = calculate_skill_score(repos)

    return {
        "username": username,
        "analytics": analytics,
        "repositories": repos
    }