import requests

def fetch_github_data(username: str):
    all_repos = []
    page = 1

    while True:
        url = f"https://api.github.com/users/{username}/repos?per_page=100&page={page}"
        response = requests.get(url)

        if response.status_code != 200:
            break

        repos = response.json()

        # If no more repos, stop loop
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
