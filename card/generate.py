#!/usr/bin/env python3
"""Render the neofetch-style profile card: dark_mode.svg and light_mode.svg.

Edit the "What the card says" block to change the text.

    GH_TOKEN=$(gh auth token) python3 card/generate.py   # fetch stats, then render
    python3 card/generate.py --offline                   # re-render from card/stats.json

The daily workflow (.github/workflows/profile-card.yml) runs the first form.
Standard library only.

Stats:
  Repos / Stars / Followers  public repos you own, their stars, your followers.
  Commits / Lines of Code    your commits and +/- lines on the default branch of
                             every non-fork repo the token can see. With the
                             workflow's built-in token that is public repos only;
                             add an ACCESS_TOKEN secret to include private ones.
  card/cache.json stores per-repo counts under hashed names, so private repo
  names never land in this public repo.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
USER = os.environ.get("GH_USER", "mirzaaghazadeh")

# ── What the card says ────────────────────────────────────────────────────────
TITLE = "navid@mirzaaghazadeh"
UPTIME_SINCE = dt.date(2016, 3, 1)  # "Uptime" counts from here: first dev job (iPros, Mar 2016)

# (key, value) pairs. None draws a bare "." spacer line.
# "{uptime}" is replaced with the time since UPTIME_SINCE.
INFO = [
    ("OS", "macOS, Linux"),
    ("Uptime", "{uptime}"),
    ("Host", "Entropol (SAFEJETS)"),
    ("Kernel", "Forward Deployed Engineer & Tech Lead"),
    ("Tools", "Docker, Nginx, Redis, AWS, Grafana"),
    None,
    ("Languages.Programming", "PHP, Python, TypeScript, Go, Swift"),
    ("Languages.Computer", "SQL, HTML, YAML, Markdown"),
    ("Languages.Real", "Persian, English, Turkish"),
    None,
    ("Focus.Backend", "Laravel, APIs, High Availability"),
    ("Focus.Security", "Bug Bounty, OWASP Top 10, Pentesting"),
]

CONTACT = [
    ("Email", "hi@navid.tr"),
    ("Website", "navid.tr"),
    ("LinkedIn", "mirzaaghazadeh"),
    ("Project.Featured", "iphone-duo-skills"),
]

# ── Layout ────────────────────────────────────────────────────────────────────
PAD = 25
FONT_SIZE = 16          # info text
LINE_HEIGHT = 20
ADVANCE = 0.6           # glyph advance of the embedded font, in em
INFO_CHARS = 62         # characters per info line, including the leading ". "
STATS_LEFT = 34         # width of the left half of a two-column stats line

ART_COLS, ART_ROWS = 60, 50   # must match make_ascii.py
ART_FONT_SIZE = 10
ART_LINE_HEIGHT = 10
GAP = 25                # between the portrait and the info column

THEMES = {
    "dark": {
        "bg": "#161b22", "text": "#c9d1d9", "art": "#c9d1d9", "key": "#ffa657",
        "value": "#a5d6ff", "cc": "#616e7f", "add": "#3fb950", "del": "#f85149",
    },
    "light": {
        "bg": "#f6f8fa", "text": "#24292f", "art": "#24292f", "key": "#953800",
        "value": "#0a3069", "cc": "#c2cfde", "add": "#1a7f37", "del": "#cf222e",
    },
}

# ── GitHub API ────────────────────────────────────────────────────────────────
API = "https://api.github.com"
MAX_WEEK_LINES = 25_000  # see contributor_totals()


def call(token: str, path: str, body: dict | None = None):
    """GET (or POST when body is given). Returns (status, parsed json or None)."""
    req = urllib.request.Request(
        path if path.startswith("http") else API + path,
        data=json.dumps(body).encode() if body is not None else None,
        method="POST" if body is not None else "GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "profile-card-generator",
        },
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as err:
            if err.code in (502, 503, 504) and attempt < 3:
                time.sleep(3 * (attempt + 1))
                continue
            return err.code, None
        except urllib.error.URLError:
            if attempt == 3:
                raise
            time.sleep(3 * (attempt + 1))
    return 0, None


def graphql(token: str, query: str, **variables) -> dict:
    status, data = call(token, "/graphql", {"query": query, "variables": variables})
    if status != 200 or not data or data.get("errors"):
        raise RuntimeError(f"GraphQL failed ({status}): {data and data.get('errors')}")
    return data["data"]


PROFILE_QUERY = """
query($login: String!, $cursor: String) {
  user(login: $login) {
    followers { totalCount }
    repositories(first: 100, after: $cursor, ownerAffiliations: OWNER, privacy: PUBLIC) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes { stargazerCount }
    }
  }
}"""


def profile_counts(token: str) -> dict:
    stars, cursor = 0, None
    while True:
        user = graphql(token, PROFILE_QUERY, login=USER, cursor=cursor)["user"]
        repos = user["repositories"]
        stars += sum(n["stargazerCount"] for n in repos["nodes"])
        if not repos["pageInfo"]["hasNextPage"]:
            break
        cursor = repos["pageInfo"]["endCursor"]
    return {"repos": repos["totalCount"], "stars": stars, "followers": user["followers"]["totalCount"]}


def list_repos(token: str) -> list[dict]:
    """Every non-fork, non-empty repo the token can see that the user may have committed to."""
    status, me = call(token, "/user")
    if status == 200 and me and me.get("login", "").lower() == USER.lower():
        base = "/user/repos?affiliation=owner,collaborator,organization_member&per_page=100"
    else:  # e.g. the workflow's GITHUB_TOKEN: public repos the user owns
        base = f"/users/{USER}/repos?type=owner&per_page=100"
    repos, page = [], 1
    while True:
        status, batch = call(token, f"{base}&page={page}")
        if status != 200:
            raise RuntimeError(f"listing repos failed ({status})")
        if not batch:
            break
        repos += batch
        page += 1
    return [r for r in repos if not r.get("fork") and r.get("size", 0) > 0]


def repo_key(repo: dict) -> str:
    """Opaque cache key: a hash of the numeric repo id, so names can't be guessed back."""
    return hashlib.sha256(f"repo:{repo['id']}".encode()).hexdigest()[:16]


def push_stamp(repo: dict) -> str:
    """Changes whenever the repo is pushed to, without publishing the push time."""
    return hashlib.sha256(f"{repo['id']}:{repo.get('pushed_at')}".encode()).hexdigest()[:12]


def contributor_totals(data: list | None) -> dict:
    for entry in data or []:
        author = entry.get("author") or {}
        if author.get("login", "").lower() == USER.lower():
            weeks = entry.get("weeks", [])
            # A week that adds or removes more than MAX_WEEK_LINES in one repo is
            # almost always vendored or generated code (node_modules, vendor/,
            # lockfiles, builds), so its lines are left out. Its commits still count.
            human = [w for w in weeks if max(w.get("a", 0), w.get("d", 0)) <= MAX_WEEK_LINES]
            return {
                "add": sum(w.get("a", 0) for w in human),
                "del": sum(w.get("d", 0) for w in human),
                "commits": sum(w.get("c", 0) for w in weeks),
            }
    return {"add": 0, "del": 0, "commits": 0}


def code_counts(token: str, cache: dict) -> tuple[dict, dict]:
    """Commits and +/- lines across repos, reusing cached repos that have not been pushed to."""
    repos = list_repos(token)
    fresh: dict = {}
    pending = []
    for repo in repos:
        key = repo_key(repo)
        old = cache.get(key)
        if old and old.get("pushed") == push_stamp(repo):
            fresh[key] = old
        else:
            pending.append(repo)

    # GitHub computes contributor stats lazily and answers 202 until they are ready.
    attempts = 8
    for attempt in range(attempts):
        waiting = []
        for repo in pending:
            key = repo_key(repo)
            status, data = call(token, f"/repos/{repo['full_name']}/stats/contributors")
            if status == 202:
                waiting.append(repo)
            elif status in (200, 204):
                fresh[key] = {"pushed": push_stamp(repo), **contributor_totals(data)}
            elif key in cache:  # transient error: keep last known numbers, retry next run
                fresh[key] = {**cache[key], "pushed": None}
        if not waiting or attempt == attempts - 1:
            break
        pending = waiting
        print(f"  waiting for GitHub to compute stats on {len(waiting)} repos…", file=sys.stderr)
        time.sleep(min(5 * (attempt + 1), 30))
    for repo in waiting:  # still not ready: fall back to the last known numbers
        key = repo_key(repo)
        if key in cache:
            fresh[key] = {**cache[key], "pushed": None}

    totals = {
        "commits": sum(v["commits"] for v in fresh.values()),
        "add": sum(v["add"] for v in fresh.values()),
        "del": sum(v["del"] for v in fresh.values()),
    }
    return totals, dict(sorted(fresh.items()))


def fetch_stats(token: str) -> dict:
    cache_file = HERE / "cache.json"
    cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
    stats = profile_counts(token)
    code, cache = code_counts(token, cache)
    stats.update(code)
    cache_file.write_text(json.dumps(cache, indent=1) + "\n")
    return stats


# ── Rendering ─────────────────────────────────────────────────────────────────
def uptime(since: dt.date, today: dt.date) -> str:
    years = today.year - since.year
    months = today.month - since.month
    days = today.day - since.day
    if days < 0:
        months -= 1
        days += (today.replace(day=1) - dt.timedelta(days=1)).day
    if months < 0:
        years -= 1
        months += 12

    def unit(n: int, word: str) -> str:
        return f"{n} {word}{'' if n == 1 else 's'}"

    return ", ".join([unit(years, "year"), unit(months, "month"), unit(days, "day")])


Seg = tuple[str, str | None]  # (text, css class or None for plain text)


def kv(key: str, value: list[Seg] | str, width: int, lead: bool = True) -> list[Seg]:
    """'. Key: ...... value' padded with dots to exactly `width` characters."""
    value_segs = [(value, "value")] if isinstance(value, str) else value
    used = (2 if lead else 0) + len(key) + 1 + sum(len(t) for t, _ in value_segs) + 2
    dots = max(width - used, 1)
    segs: list[Seg] = [(". ", "cc")] if lead else []
    return segs + [(key, "key"), (":", None), (" " + "." * dots + " ", "cc")] + value_segs


def rule(label: str, width: int, prefix: str = "— ") -> list[Seg]:
    head = f"{prefix}{label} "
    return [(head + "—" * max(width - len(head), 0), None)]


def info_lines(stats: dict, today: dt.date) -> list[list[Seg]]:
    W = INFO_CHARS
    lines: list[list[Seg]] = [rule(TITLE, W, prefix="")]
    for item in INFO:
        if item is None:
            lines.append([(".", "cc")])
            continue
        key, value = item
        lines.append(kv(key, value.replace("{uptime}", uptime(UPTIME_SINCE, today)), W))

    lines += [[], rule("Contact", W)]
    lines += [kv(k, v, W) for k, v in CONTACT]

    n = lambda x: f"{x:,}"  # noqa: E731
    right = W - STATS_LEFT - 3
    lines += [[], rule("GitHub Stats", W)]
    lines.append(kv("Repos", n(stats["repos"]), STATS_LEFT) + [(" | ", "cc")]
                 + kv("Stars", n(stats["stars"]), right, lead=False))
    lines.append(kv("Commits", n(stats["commits"]), STATS_LEFT) + [(" | ", "cc")]
                 + kv("Followers", n(stats["followers"]), right, lead=False))
    loc = [
        (n(stats["add"] - stats["del"]), "value"), (" ( ", None),
        (n(stats["add"]) + "++", "add"), (", ", None),
        (n(stats["del"]) + "--", "del"), (" )", None),
    ]
    loc_len = sum(len(t) for t, _ in loc)
    # Use the longest label that still leaves room for a few dots.
    label = next((lb for lb in ("Lines of Code on GitHub", "Lines of Code", "LOC")
                  if 2 + len(lb) + 1 + 2 + 3 + loc_len <= W), "LOC")
    lines.append(kv(label, loc, W))
    return lines


def render(theme: str, stats: dict, today: dt.date, font_b64: str) -> str:
    c = THEMES[theme]
    art = (HERE / f"ascii-{theme}.txt").read_text(encoding="utf-8").splitlines()
    lines = info_lines(stats, today)

    art_w = ART_COLS * ART_FONT_SIZE * ADVANCE
    art_h = ART_ROWS * ART_LINE_HEIGHT
    info_w = INFO_CHARS * FONT_SIZE * ADVANCE
    info_h = len(lines) * LINE_HEIGHT
    width = round(PAD + art_w + GAP + info_w + PAD)
    height = round(PAD + max(art_h, info_h) + PAD)
    info_x = PAD + art_w + GAP

    # Centre both columns vertically; baselines sit ~0.8em below each line's top.
    art_top = (height - art_h) / 2
    info_top = (height - info_h) / 2

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" xml:space="preserve" '
        f'font-family="CardMono, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace" '
        f'font-size="{FONT_SIZE}px">',
        f"<title>{escape(TITLE)} — GitHub profile card</title>",
        "<style>",
        f"@font-face{{font-family:CardMono;src:url(data:font/woff2;base64,{font_b64}) format('woff2');}}",
        "text,tspan{white-space:pre;font-variant-ligatures:none;font-feature-settings:'liga' 0,'calt' 0}",
        f".t{{fill:{c['text']}}}.key{{fill:{c['key']}}}.value{{fill:{c['value']}}}"
        f".cc{{fill:{c['cc']}}}.add{{fill:{c['add']}}}.del{{fill:{c['del']}}}"
        f".art{{fill:{c['art']};font-size:{ART_FONT_SIZE}px}}",
        "</style>",
        f'<rect width="{width}" height="{height}" rx="15" fill="{c["bg"]}"/>',
    ]

    for i, row in enumerate(art):
        if row.strip():
            y = art_top + i * ART_LINE_HEIGHT + ART_FONT_SIZE * 0.8
            out.append(f'<text class="art" x="{PAD}" y="{y:.1f}">{escape(row)}</text>')

    for i, segs in enumerate(lines):
        if not segs:
            continue
        y = info_top + i * LINE_HEIGHT + FONT_SIZE * 0.8
        spans = "".join(
            f'<tspan class="{cls}">{escape(text)}</tspan>' if cls else escape(text)
            for text, cls in segs
        )
        out.append(f'<text class="t" x="{info_x:.1f}" y="{y:.1f}">{spans}</text>')

    out.append("</svg>")
    return "\n".join(out) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="Render the profile card SVGs.")
    ap.add_argument("--offline", action="store_true", help="skip the API; reuse card/stats.json")
    args = ap.parse_args()

    stats_file = HERE / "stats.json"
    if args.offline:
        stats = json.loads(stats_file.read_text())
    else:
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not token:
            sys.exit("set GH_TOKEN (e.g. GH_TOKEN=$(gh auth token)) or pass --offline")
        stats = fetch_stats(token)
        stats_file.write_text(json.dumps(stats, indent=2) + "\n")

    today = dt.datetime.now(dt.timezone.utc).date()
    font_b64 = base64.b64encode((HERE / "font.woff2").read_bytes()).decode()
    for theme in THEMES:
        (ROOT / f"{theme}_mode.svg").write_text(render(theme, stats, today, font_b64), encoding="utf-8")
    print(json.dumps(stats))


if __name__ == "__main__":
    main()
