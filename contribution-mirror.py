#!/usr/bin/env python3
"""
contribution-mirror.py

Reproduce the GitHub contribution graph of one account (work) onto another
(personal) using backdated empty commits. No source code is ever cloned or
copied -- only the per-day contribution COUNTS are read via the GraphQL API.

Config comes from .env (git-ignored). See .env for the fields.

Both graphs are read by account NAME through the public user() API, not by
whose token is presented, so the single API_TOKEN is only there to authenticate
the request -- any valid token does, including the Actions GITHUB_TOKEN. That
is what lets the scheduled workflow run with no PAT stored anywhere. The cost
is a dependency on each account having "Include private contributions on my
profile" enabled; a guard below fails loudly if the source graph ever loses
that visibility, rather than silently mirroring nothing.

Usage -- the py launcher cannot resolve this file's shebang on this machine,
so call the uv-managed interpreter directly:

    PY=~/AppData/Roaming/uv/python/cpython-3.12.11-windows-x86_64-none/python.exe
    $PY contribution-mirror.py            # dry run: print what would be created
    $PY contribution-mirror.py --apply    # create the commits
    $PY contribution-mirror.py --repo X   # operate on the git checkout at X
                                          # (e.g. a worktree on the mirror
                                          # branch, when this file's checkout
                                          # is on the tooling branch)

Mirroring tops UP, never sums. For every day the script compares the two
accounts and creates only the DIFFERENCE, max(0, work - personal), so the
personal graph ends up showing whichever account contributed more that day.
A day where the personal account already out-contributed work gets nothing.
Today is always skipped -- it is still accumulating on both sides and would
otherwise be frozen at a partial count.

Once a branch has been published, the script only APPENDS: days up to the
last published mirror commit are kept exactly as pushed (changing them would
orphan commits, and GitHub keeps crediting orphaned commits forever, which
permanently inflates the graph). New days are committed on top of the
published tip, so a re-run is always a fast-forward push and the graph gains
only what is genuinely new. With no published branch yet, the whole history
is built from scratch as a fresh root.

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

# Reads a named account rather than the token's own account. Verified to return
# byte-identical per-day counts to the viewer{} form for both accounts: private
# contributions are folded into the daily squares (reported separately as
# restrictedContributionsCount) as long as that account has "Include private
# contributions on my profile" enabled. That is what lets this run on any token,
# including the Actions GITHUB_TOKEN, with no PAT stored anywhere.
QUERY = """
query($login:String!, $from:DateTime!, $to:DateTime!){
  user(login:$login){
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
    # Any token works -- it only authenticates the read, it does not determine
    # whose graph is read. DEST_TOKEN is accepted as a fallback so an older
    # .env keeps working.
    if not cfg.get("API_TOKEN"):
        cfg["API_TOKEN"] = cfg.get("DEST_TOKEN") or cfg.get("WORK_TOKEN", "")
    missing = [k for k in ("API_TOKEN", "WORK_LOGIN", "DEST_LOGIN",
                           "PERSONAL_EMAIL") if not cfg.get(k)]
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


def check_account(token, login, label):
    status, body = api(f"{REST}/users/{login}", token)
    if status == 401:
        raise SystemExit(f"API_TOKEN rejected by GitHub (401). Any valid token "
                         f"works; it only authenticates the read.")
    if status != 200:
        raise SystemExit(f"{label} account '{login}' not readable ({status}: "
                         f"{body.get('message', 'unknown')}). Check {label} in .env.")
    return body


# ------------------------------------------------------------- the graph ----

def fetch_year(token, login, year, who):
    now = datetime.now(timezone.utc)
    start = f"{year}-01-01T00:00:00Z"
    end = f"{year}-12-31T23:59:59Z"
    if year == now.year:                       # don't ask for future dates
        end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    if year > now.year:
        return []

    status, payload = api(GRAPHQL, token, "POST", {
        "query": QUERY,
        "variables": {"login": login, "from": start, "to": end}})
    if status != 200 or "errors" in payload:
        raise SystemExit(f"GraphQL error for {login} {year}: "
                         f"{payload.get('errors') or payload}")
    if not payload["data"].get("user"):
        raise SystemExit(f"No such GitHub account: '{login}'.")

    coll = payload["data"]["user"]["contributionsCollection"]
    cal = coll["contributionCalendar"]
    restricted = coll["restrictedContributionsCount"]
    print(f"  {year} ({who}: {login}): {cal['totalContributions']} counted, "
          f"{restricted} of them private")

    return [(d["date"], d["contributionCount"])
            for w in cal["weeks"] for d in w["contributionDays"]
            if d["contributionCount"] > 0]


def fetch_days(token, login, years, who):
    days = {}
    for year in years:
        days.update(fetch_year(token, login, year, who))
    return days


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


def published_tip(branch):
    """Return (sha, last_published_date) for origin/<branch>, or (None, None)."""
    git("fetch", "--quiet", "origin", branch)
    sha = git("rev-parse", "--verify", "--quiet",
              "refs/remotes/origin/" + branch).stdout.strip()
    if not sha:
        return None, None
    dates = git("log", sha, "--pretty=%as").stdout.split()
    return sha, max(dates) if dates else None


def start_empty_branch():
    """Throw away all history so the next commit becomes a fresh root commit.

    Only used when nothing is published yet. No placeholder root commit is
    created: the first backdated mirror commit is the root. An extra root
    would land on today's date and show up as a contribution that isn't in
    the source graph.
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
    global REPO_DIR

    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually create the commits")
    ap.add_argument("--repo", default=REPO_DIR,
                    help="git checkout to operate on (default: this script's "
                         "directory); point it at a worktree on the mirror "
                         "branch when this checkout is on the tooling branch")
    args = ap.parse_args()
    REPO_DIR = os.path.abspath(args.repo)

    cfg = load_env(ENV_PATH)
    preflight()

    work_login, dest_login = cfg["WORK_LOGIN"], cfg["DEST_LOGIN"]

    # The bug that inflated the graph to 175+/day: reading the destination's own
    # calendar as the source feeds back the empty commits the last run created
    # and doubles every day. Refuse outright.
    if work_login.lower() == dest_login.lower():
        raise SystemExit(
            f"ABORT: WORK_LOGIN and DEST_LOGIN are both '{work_login}'. "
            f"That mirrors the account onto itself and doubles every day's "
            f"count on every run.")

    token = cfg["API_TOKEN"]
    check_account(token, work_login, "WORK_LOGIN")
    check_account(token, dest_login, "DEST_LOGIN")
    email = cfg["PERSONAL_EMAIL"]
    print(f"Source (work)         : {work_login}")
    print(f"Destination (personal): {dest_login}")
    print(f"Authoring commits as  : {cfg['PERSONAL_NAME']} <{email}>")

    print("Reading source graph:")
    work_days = fetch_days(token, work_login, cfg["YEARS"], "work")
    if not work_days:
        raise SystemExit("No contributions returned -- check WORK_LOGIN and YEARS.")

    print("Reading destination graph:")
    personal_days = fetch_days(token, dest_login, cfg["YEARS"], "personal")

    branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main"
    prev, last_pub = published_tip(branch)

    # Today is still accumulating on both sides -- mirroring it now would
    # freeze it at a partial count, so it always waits for the next run.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Days at or before the last published mirror commit are frozen exactly as
    # pushed. For the rest, mirror only the shortfall: the personal calendar
    # for those days cannot contain mirror commits yet, so its count is the
    # account's own (native) activity and max(0, work - personal) tops the day
    # up to whichever account contributed more.
    def shortfall(date):
        return max(0, work_days[date] - personal_days.get(date, 0))

    floor = last_pub or ""

    # The source graph is read through the public user() API, which includes an
    # account's private contributions only while that account has "Include
    # private contributions on my profile" switched on. If that is ever turned
    # off on the work account, this would see a near-empty calendar, compute a
    # shortfall of zero everywhere and silently stop mirroring -- safe, but
    # invisible for weeks. Compare what work still reports for the already-
    # frozen period against what has actually been mirrored, and fail loudly.
    if prev:
        mirrored = set(git("log", prev, "--pretty=%as").stdout.split())
        frozen_work = [d for d in work_days if d <= floor]
        if mirrored and len(frozen_work) * 2 < len(mirrored):
            raise SystemExit(
                f"ABORT: {work_login} now reports only {len(frozen_work)} "
                f"active day(s) on or before {floor}, but {len(mirrored)} "
                f"day(s) have already been mirrored. The source graph has lost "
                f"visibility -- most likely 'Include private contributions on "
                f"my profile' was switched off on {work_login}. Refusing to run "
                f"against a partial view.")

    todo = [(d, shortfall(d)) for d in sorted(work_days)
            if d > floor and d < today and shortfall(d) > 0]
    skipped_even = [d for d in sorted(work_days)
                    if d > floor and d < today and shortfall(d) == 0]

    if prev:
        frozen = [d for d in work_days if d <= floor]
        print(f"\nPublished through {last_pub} -- {len(frozen)} work-active "
              f"day(s) frozen as pushed; appending only after that.")
    if skipped_even:
        print(f"{len(skipped_even)} day(s) need nothing -- the personal account "
              f"already matches or beats work there: {skipped_even[:5]}")

    if not todo:
        print("\nNothing to mirror: every new day is already covered.")
        return

    cap = cfg["COMMITS_CAP"]
    planned = sum(min(n, cap) for _, n in todo)
    capped = [(d, n) for d, n in todo if n > cap]
    print(f"\n{'date':<12} {'work':>5} {'personal':>9} {'mirror':>7}")
    for d, n in todo:
        print(f"{d:<12} {work_days[d]:>5} {personal_days.get(d, 0):>9} "
              f"{min(n, cap):>7}")
    print(f"\n{len(todo)} day(s) to top up, {planned} commits to create "
          f"(cap {cap}/day).")
    if capped:
        print(f"  {len(capped)} day(s) exceed the cap and will be truncated: "
              f"{capped[:5]}")

    if not args.apply:
        print("\nDry run. Re-run with --apply to create the commits.")
        return

    print()
    if prev:
        # Build strictly on the published tip so the push is a fast-forward
        # and nothing already credited is ever orphaned.
        subprocess.run(["git", "-C", REPO_DIR, "checkout", "-B", branch, prev],
                       check=True, capture_output=True)
        created = make_commits(todo, cfg["PERSONAL_NAME"], email, cap)
        print(f"Created {created} commits on '{branch}' on top of the "
              f"published tip.")
    else:
        print("Nothing published yet -- building the whole branch from scratch...")
        branch = start_empty_branch()
        created = make_commits(todo, cfg["PERSONAL_NAME"], email, cap)
        finish_branch(branch)
        print(f"Created {created} commits on '{branch}'.")
        print()
        print("Push with:")
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
