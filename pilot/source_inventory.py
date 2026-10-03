"""Resolve upstream commit IDs through official, read-only endpoints only."""

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import quote
from urllib.request import Request, urlopen


GIT_TIMEOUT_SECONDS = 8
HTTP_TIMEOUT_SECONDS = 6
MAX_RESPONSE_BYTES = 2_000_000
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
COMMIT_LINK_PATTERN = re.compile(
    r"href=[\"'](?:https://github\.com)?/[^\"'<>]+/commit/([0-9a-f]{40})(?:[/?#][^\"']*)?[\"']",
    re.IGNORECASE,
)
DEFAULT_BRANCH_PATTERN = re.compile(
    r"(?:defaultBranch|default_branch)[\"']?\s*:\s*[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)

UPSTREAMS = {
    "immich": {
        "repo_url": "https://github.com/immich-app/immich.git",
        "provider": "github",
        "web_url": "https://github.com/immich-app/immich",
        "api_url": "https://api.github.com/repos/immich-app/immich",
    },
    "digikam": {
        "repo_url": "https://invent.kde.org/graphics/digikam.git",
        "provider": "kde",
        "web_url": "https://invent.kde.org/graphics/digikam",
        "manual_check_url": "https://invent.kde.org/graphics/digikam/-/commits/master",
    },
    "ai_file_sorter": {
        "repo_url": "https://github.com/hyperfield/ai-file-sorter.git",
        "provider": "github",
        "web_url": "https://github.com/hyperfield/ai-file-sorter",
        "api_url": "https://api.github.com/repos/hyperfield/ai-file-sorter",
    },
}


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_head(output):
    """Return the single full SHA reported for HEAD or reject the response."""
    matches = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1] == "HEAD":
            matches.append(fields[0])
    if len(matches) != 1 or not SHA_PATTERN.fullmatch(matches[0]):
        raise ValueError("expected one full 40-character SHA for HEAD")
    return matches[0]


def _http_get(url, timeout=HTTP_TIMEOUT_SECONDS):
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json, text/html;q=0.9, */*;q=0.8",
            "User-Agent": "local-photo-studio-feasibility-probe",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("official response exceeded the metadata size limit")
    return body


def _parse_json_object(body):
    data = json.loads(body.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data


def _verify_sha(value):
    if not isinstance(value, str) or not SHA_PATTERN.fullmatch(value):
        raise ValueError("official endpoint did not provide a full 40-character SHA")
    return value


def _verified_result(name, source, commit, method, evidence_url, checked_at, errors=None):
    return {
        "project": name,
        "repository_url": source["repo_url"],
        "status": "verified",
        "commit": _verify_sha(commit),
        "method": method,
        "evidence_url": evidence_url,
        "checked_at": checked_at,
        "error": "; ".join(errors) if errors else None,
        "manual_check_url": source.get("manual_check_url", source["web_url"] + "/commits"),
    }


def _github_api_revision(source, http_get):
    repository = _parse_json_object(http_get(source["api_url"], timeout=HTTP_TIMEOUT_SECONDS))
    branch = repository.get("default_branch")
    if not isinstance(branch, str) or not branch:
        raise ValueError("GitHub repository response has no default_branch")
    commit_url = source["api_url"] + "/commits/" + quote(branch, safe="/")
    commit_data = _parse_json_object(http_get(commit_url, timeout=HTTP_TIMEOUT_SECONDS))
    return _verify_sha(commit_data.get("sha")), commit_url, branch


def _github_web_revision(source, http_get, branch=None):
    repository_html = http_get(source["web_url"], timeout=HTTP_TIMEOUT_SECONDS).decode("utf-8", "replace")
    if branch is None:
        match = DEFAULT_BRANCH_PATTERN.search(repository_html)
        if not match:
            raise ValueError("official repository page did not identify its default branch")
        branch = match.group(1)
    branch_path = quote(branch, safe="/-._")
    commits_url = source["web_url"] + "/commits/" + branch_path
    commits_html = http_get(commits_url, timeout=HTTP_TIMEOUT_SECONDS).decode("utf-8", "replace")
    match = COMMIT_LINK_PATTERN.search(commits_html)
    if not match:
        raise ValueError("official commits page did not expose a complete commit link")
    return _verify_sha(match.group(1)), commits_url


def resolve_source(name, git_runner=None, http_get=None, clock=None):
    """Resolve one source independently; never downloads a repository checkout."""
    if name not in UPSTREAMS:
        raise KeyError("unknown upstream: " + name)
    source = UPSTREAMS[name]
    git_runner = git_runner or subprocess.run
    http_get = http_get or _http_get
    clock = clock or _utc_now
    checked_at = clock()
    errors = []

    try:
        result = git_runner(
            ["git", "ls-remote", source["repo_url"], "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "git ls-remote failed")
        sha = parse_head(result.stdout)
        return _verified_result(name, source, sha, "git_ls_remote", source["repo_url"], checked_at)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        errors.append("git: " + str(exc))

    if source["provider"] == "github":
        branch = None
        try:
            sha, evidence_url, branch = _github_api_revision(source, http_get)
            return _verified_result(name, source, sha, "github_api", evidence_url, checked_at, errors)
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            errors.append("GitHub API: " + str(exc))
        try:
            sha, evidence_url = _github_web_revision(source, http_get, branch=branch)
            return _verified_result(name, source, sha, "official_web", evidence_url, checked_at, errors)
        except (OSError, ValueError) as exc:
            errors.append("official GitHub page: " + str(exc))

    return {
        "project": name,
        "repository_url": source["repo_url"],
        "status": "unverified",
        "commit": None,
        "method": None,
        "evidence_url": None,
        "checked_at": checked_at,
        "error": "; ".join(errors),
        "manual_check_url": source.get("manual_check_url", source["web_url"] + "/commits"),
    }


def inventory(git_runner=None, http_get=None, clock=None):
    generated_at = (clock or _utc_now)()
    return {
        "generated_at": generated_at,
        "sources": {
            name: resolve_source(name, git_runner=git_runner, http_get=http_get, clock=lambda: generated_at)
            for name in UPSTREAMS
        },
    }


def main():
    report = inventory()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if any(source["status"] != "verified" for source in report["sources"].values()) else 0


if __name__ == "__main__":
    sys.exit(main())
