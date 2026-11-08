#!/usr/bin/env python3
"""
contribution-mirror.py

Reproduce the GitHub contribution graph of one account (work) onto another
(personal) using backdated empty commits. No source code is ever cloned or
copied -- only the per-day contribution COUNTS are read via the GraphQL API.

Config and tokens come from .env (git-ignored). See .env for the fields.

Usage -- the py launcher cannot resolve this file's shebang on this machine,
so call the uv-managed interpreter directly:

    PY=~/AppData/Roaming/uv/python/cpython-3.12.11-windows-x86_64-none/python.exe
    $PY contribution-mirror.py            # dry run: print what would be created
    $PY contribution-mirror.py --apply    # rebuild the branch and create commits

The branch is always rebuilt from scratch and commit hashes are deterministic,
so re-running is safe. An unchanged source graph reproduces byte-identical
commits; a source graph that has only gained recent days reproduces every old
commit unchanged and appends the new ones. Either way nothing is orphaned and
the contribution graph gains only what is genuinely new.

If an already-mirrored day's count changes, though, every commit dated after it
gets a new hash. GitHub keeps crediting the displaced commits forever, so that
case inflates the graph. After rebuilding, the script checks for exactly this
and tells you when a plain push is safe versus when the repo must be recreated.

Requires nothing outside the standard library.
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = HERE
ENV_PATH = os.path.join(HERE, ".env")

GRAPHQL = "https://api.github.com/graphql"
REST = "https://api.github.com"

QUERY = """
query($from:DateTime!, $to:DateTime!){
  viewer{
    login
    contributionsCollection(from:$from, to:$to){
      restrictedContributionsCount
      contributionCalendar{
        totalContributions
        weeks{ contributionDays{ date contributionCount } }
      }
    }
  }
}"""


# ---------------------------------------------------------------- config ----

def load_env(path):
    if not os.path.exists(path):
        raise SystemExit(f"Missing {path}. Copy the template from the repo README "
                         f"and fill in your tokens.")
    cfg = {}
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            cfg[key.strip()] = val.strip()
    missing = [k for k in ("WORK_TOKEN", "DEST_TOKEN") if not cfg.get(k)]
    if missing:
        raise SystemExit(f"{path} is missing: {', '.join(missing)}")
    cfg.setdefault("PERSONAL_NAME", "Dran")
    cfg["YEARS"] = [int(y) for y in cfg.get("YEARS", "2026").replace(
        "[", "").replace("]", "").split(",") if y.strip()]
    cfg["COMMITS_CAP"] = int(cfg.get("COMMITS_CAP", "20"))
    return cfg


# ------------------------------------------------------------------ http ----

def api(url, token, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.load(exc)
        except Exception:
            return exc.code, {}


def whoami(token, label):
    status, body = api(f"{REST}/user", token)
    if status != 200:
        raise SystemExit(f"{label} token rejected by GitHub ({status}: "
                         f"{body.get('message', 'unknown')}). Generate a new PAT "
                         f"and update .env.")
    return body


# ------------------------------------------------------------- the graph ----

def fetch_year(token, year):
    now = datetime.now(timezone.utc)
    start = f"{year}-01-01T00:00:00Z"
    end = f"{year}-12-31T23:59:59Z"
    if year == now.year:                       # don't ask for future dates
        end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    if year > now.year:
        return []

    status, payload = api(GRAPHQL, token, "POST", {
        "query": QUERY, "variables": {"from": start, "to": end}})
    if status != 200 or "errors" in payload:
        raise SystemExit(f"GraphQL error for {year}: "
                         f"{payload.get('errors') or payload}")

    coll = payload["data"]["viewer"]["contributionsCollection"]
    cal = coll["contributionCalendar"]
    restricted = coll["restrictedContributionsCount"]
    print(f"  {year}: {cal['totalContributions']} counted, "
          f"{restricted} restricted (hidden)")
    if restricted:
        print(f"      ^ {restricted} private/org contributions can't be dated -- "
              f"the work token needs repo + read:org scope and SSO authorization "
              f"to see them.")

    return [(d["date"], d["contributionCount"])
            for w in cal["weeks"] for d in w["contributionDays"]
            if d["contributionCount"] > 0]


def resolve_email(token, dest_user):
    """Pick a verified email on the destination account. The noreply address is
    always linked and always counts, so it is the guaranteed fallback."""
    noreply = f"{dest_user['id']}+{dest_user['login']}@users.noreply.github.com"

    status, body = api(f"{REST}/user/emails", token)
    if status != 200:
        print(f"  (token lacks 'user:email' scope -- using noreply)")
        return noreply

    verified = [e["email"] for e in body if e["verified"]]
    for cand in verified:
        if cand.lower().endswith("users.noreply.github.com"):
            continue
        return cand
    return noreply


# -------------------------------------------------------------------- git ---

def git(*args, **kw):
    return subprocess.run(["git", "-C", REPO_DIR, *args],
                          capture_output=True, text=True, **kw)


def preflight():
    if git("rev-parse", "--git-dir").returncode != 0:
        raise SystemExit(f"'{REPO_DIR}' is not a git repo.")
    dirty = git("status", "--porcelain", "--untracked-files=no").stdout.strip()
    if dirty:
        raise SystemExit("Working tree has staged/modified tracked files. "
                         "Commit or stash them first:\n" + dirty)


def start_empty_branch():
    """Throw away all history so the next commit becomes a fresh root commit.

    This is what makes the script idempotent. The previous version appended to
    whatever was already there, so every re-run stacked another COMMITS_CAP
    commits onto every active day.

    No placeholder root commit is created: the first backdated mirror commit is
    the root. An extra root would land on today's date and show up as a
    contribution that isn't in the source graph.
    """
    branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main"
    subprocess.run(["git", "-C", REPO_DIR, "checkout", "--orphan", "_rebuild"],
                   check=True, capture_output=True)
    git("rm", "-rf", "--cached", ".")
    git("branch", "-D", branch)
    return branch


def finish_branch(branch):
    subprocess.run(["git", "-C", REPO_DIR, "branch", "-m", branch],
                   check=True, capture_output=True)


def make_commits(days, name, email, cap):
    total = 0
    for date, count in days:
        for i in range(min(count, cap)):
            stamp = f"{date}T12:{i:02d}:00"
            env = dict(os.environ,
                       GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp,
                       GIT_AUTHOR_NAME=name, GIT_COMMITTER_NAME=name,
                       GIT_AUTHOR_EMAIL=email, GIT_COMMITTER_EMAIL=email)
            subprocess.run(["git", "-C", REPO_DIR, "commit", "--allow-empty",
                            "-m", f"mirror {date} #{i + 1}"],
                           env=env, check=True, stdout=subprocess.DEVNULL)
            total += 1
    return total


# ------------------------------------------------------------------- main ---

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually rewrite the branch and create commits")
    args = ap.parse_args()

    cfg = load_env(ENV_PATH)
    preflight()

    work = whoami(cfg["WORK_TOKEN"], "WORK_TOKEN")
    dest = whoami(cfg["DEST_TOKEN"], "DEST_TOKEN")
    print(f"Source (work)      : {work['login']}")
    print(f"Destination (personal): {dest['login']}")

    # The bug that inflated the graph to 175+/day: if both tokens point at the
    # same account, the script reads the destination's own calendar -- including
    # the empty commits it created last run -- and doubles it. Refuse outright.
    if work["login"].lower() == dest["login"].lower():
        raise SystemExit(
            f"\nABORT: WORK_TOKEN and DEST_TOKEN both belong to '{work['login']}'.\n"
            f"That mirrors the account onto itself and doubles every day's count\n"
            f"on every run. Point WORK_TOKEN at the work account.")

    email = resolve_email(cfg["DEST_TOKEN"], dest)
    print(f"Authoring commits as: {cfg['PERSONAL_NAME']} <{email}>")

    print("Reading source graph:")
    days = []
    for year in cfg["YEARS"]:
        days += fetch_year(cfg["WORK_TOKEN"], year)
    days.sort()

    if not days:
        raise SystemExit("No contributions returned -- check WORK_TOKEN and YEARS.")

    cap = cfg["COMMITS_CAP"]
    planned = sum(min(n, cap) for _, n in days)
    capped = [(d, n) for d, n in days if n > cap]
    print(f"\n{len(days)} active days, {sum(n for _, n in days)} contributions "
          f"in source; {planned} commits to create (cap {cap}/day).")
    if capped:
        print(f"  {len(capped)} day(s) exceed the cap and will be truncated: "
              f"{capped[:5]}")

    if not args.apply:
        print("\nDry run. Re-run with --apply to rebuild the branch.")
        return

    # Remember what is already published, so we can tell afterwards whether the
    # rebuild is a fast-forward. This matters: GitHub never retracts a
    # contribution for a commit once it has been pushed, even after that commit
    # is force-pushed out of the branch. Orphaning commits therefore inflates
    # the graph permanently, and only deleting the repo can undo it.
    current = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main"
    git("fetch", "--quiet", "origin", current)
    prev = git("rev-parse", "--verify", "--quiet",
               "refs/remotes/origin/" + current).stdout.strip()

    print()
    print("Discarding old history and rebuilding...")
    branch = start_empty_branch()
    created = make_commits(days, cfg["PERSONAL_NAME"], email, cap)
    finish_branch(branch)
    print(f"Created {created} commits on '{branch}'.")

    if not prev:
        print()
        print("Nothing published yet. Push with:")
        print(f"  git -C . push -u origin {branch}")
        return

    ff = subprocess.run(["git", "-C", REPO_DIR, "merge-base",
                         "--is-ancestor", prev, "HEAD"]).returncode == 0
    if ff:
        added = git("rev-list", "--count", prev + "..HEAD").stdout.strip()
        print()
        print(f"Fast-forward: all published commits preserved, {added} new on top.")
        print(f"Nothing orphaned, so the graph gains only the new days.")
        print(f"  git -C . push origin {branch}")
    else:
        lost = git("rev-list", "--count", "HEAD.." + prev).stdout.strip()
        print()
        print("*** WARNING: history diverged from what is already published. ***")
        print(f"  {lost} already-pushed commit(s) would be orphaned. GitHub keeps")
        print(f"  crediting orphaned commits, so force-pushing would inflate the")
        print(f"  graph by about that much, permanently.")
        print(f"  Fix: delete the repo on GitHub, recreate it empty, then push.")


if __name__ == "__main__":
    sys.exit(main())
