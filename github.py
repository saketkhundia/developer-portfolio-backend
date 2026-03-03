import requests
import os

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")

def get_headers():
    headers = {"Accept": "application/vnd.github.v3+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"
    return headers

def fetch_github_data(username: str):
    all_repos = []
    page = 1

    while True:
        url = f"https://api.github.com/users/{username}/repos?per_page=100&page={page}"
        response = requests.get(url, headers=get_headers())

        if response.status_code != 200:
            break

        repos = response.json()

        if not repos:
            break

        for repo in repos:
            all_repos.append({
                "name": repo["name"],
                "stars": repo["stargazers_count"],
                "forks": repo["forks_count"],
                "language": repo["language"],
                "updated_at": repo["updated_at"]
            })

        page += 1

    return all_repos