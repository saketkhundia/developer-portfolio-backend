from collections import Counter
from datetime import datetime
import math

def calculate_skill_score(repos):
    total_stars    = sum(r["stars"] for r in repos)
    total_forks    = sum(r["forks"] for r in repos)
    total_projects = len(repos)

    languages      = [r["language"] for r in repos if r["language"]]
    language_count = Counter(languages)
    most_used_language = language_count.most_common(1)[0][0] if language_count else None

    current_year   = datetime.now().year
    recent_projects = sum(1 for r in repos if int(r["updated_at"][:4]) == current_year)

    # ── Each component is independently capped at its max points ──
    #
    # Stars:    log scale so 1 star ≠ same as 1000 stars linearly
    #           log2(stars+1) / log2(1001) * 35  → max 35 pts at ~1000 stars
    star_pts   = min(35, math.log2(total_stars + 1) / math.log2(1001) * 35)

    # Forks:    similar log scale, max 20 pts at ~500 forks
    fork_pts   = min(20, math.log2(total_forks + 1) / math.log2(501) * 20)

    # Repos:    diminishing returns after 20 repos, max 20 pts
    repo_pts   = min(20, (total_projects / 20) * 20)

    # Recency:  active this year, max 15 pts
    recent_pts = min(15, (recent_projects / 10) * 15)

    # Diversity: number of languages used, max 10 pts
    lang_pts   = min(10, len(language_count) * 1.5)

    skill_score = round(star_pts + fork_pts + repo_pts + recent_pts + lang_pts)
    skill_score = min(100, skill_score)  # hard cap

    return {
        "total_projects":        total_projects,
        "total_stars":           total_stars,
        "total_forks":           total_forks,
        "recent_projects":       recent_projects,
        "most_used_language":    most_used_language,
        "language_distribution": dict(language_count),
        "skill_score":           skill_score,
        # breakdown for transparency
        "_score_breakdown": {
            "stars":    round(star_pts),
            "forks":    round(fork_pts),
            "repos":    round(repo_pts),
            "recency":  round(recent_pts),
            "diversity":round(lang_pts),
        }
    }