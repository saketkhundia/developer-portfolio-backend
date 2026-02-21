from collections import Counter
from datetime import datetime

def calculate_skill_score(repos):

    total_stars = sum(repo["stars"] for repo in repos)
    total_forks = sum(repo["forks"] for repo in repos)
    total_projects = len(repos)

    # Language analysis
    languages = [repo["language"] for repo in repos if repo["language"]]
    language_count = Counter(languages)

    # Most used language
    most_used_language = None
    if language_count:
        most_used_language = language_count.most_common(1)[0][0]

    # Activity score (based on recent updates)
    current_year = datetime.now().year
    recent_projects = 0

    for repo in repos:
        year = int(repo["updated_at"][:4])
        if year == current_year:
            recent_projects += 1

    # Professional scoring formula
    skill_score = (
        total_stars * 5 +
        total_forks * 3 +
        total_projects * 4 +
        recent_projects * 6
    )

    return {
        "total_projects": total_projects,
        "total_stars": total_stars,
        "total_forks": total_forks,
        "recent_projects": recent_projects,
        "most_used_language": most_used_language,
        "language_distribution": dict(language_count),
        "skill_score": skill_score
    }
