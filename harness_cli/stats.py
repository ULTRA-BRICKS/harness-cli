"""`harness stats` — per-product-ticket review/fix iteration counts, follow-up chains, and
best-effort local-transcript token usage.

Reached at runtime via `harness stats <ticket-ref> [--repo R] [--all] [--json]`, lazy-imported
from `harness_cli/cli.py`'s `_run_stats` so `harness --help` stays on the stdlib-only fast path
(this module imports `subprocess` and reaches into `harness_cli.search`/`harness_cli.transcripts`
at call time only).

Three independent, best-effort data sources, each with its own fail-closed reason field — never
a fabricated number, never a silent zero standing in for "unknown":

1. **Follow-up chain** — shells out (read-only) to the sibling `task` CLI (task-cli):
   `task read <id> --repo <owner/repo> --json`. That ticket's `links` dict (task-cli
   deploy-followup-links) carries a `"Followed up by #<id>"` key for every ticket that links
   back `follow-up-of` this one; this module greps those exact key strings (mirroring task-cli's
   own `links.linked_other_ids`) rather than depending on any task-cli Python import — the two
   repos share only the `task` binary's stdout contract, never a module boundary.
2. **Review iterations** — shells out (read-only) to `gh`: `gh pr list --search "<id> in:body"`
   to find the PR(s) referencing the ticket (task-cli's PR bodies say `Refs #N`, never
   `Closes`/`Fixes`), then `gh pr view <n> --json reviews,commits` per match. The count is the
   number of `CHANGES_REQUESTED` reviews across those PRs — a documented, imperfect proxy for
   "how many review rounds did this take" (see `count_review_iterations`'s docstring for its
   known blind spot).
3. **Token usage** — reuses `harness_cli.search`'s existing local Claude Code/OMP transcript
   scan (never a second implementation of transcript discovery) to find parent sessions whose
   user messages mention the ticket id or its title, then sums `harness_cli.transcripts`'
   real, observed `SessionUsage` token fields across the matches. No estimation, ever.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from harness_cli.report import _sanitize

_REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")

# The exact key strings task-cli's `task link <followup> follow-up-of <original>` writes into
# the ORIGINAL ticket's `links` dict (task-cli deploy-followup-links; see tasklib/links.py's
# `_LABELS["followed-up-by"] == "Followed up by"`) - locked in with that slice, see this
# ticket's shared context.
_FOLLOWED_UP_BY_RE = re.compile(r"^Followed up by #(\d+)$")

# Review-iteration bucket labels for the `--all` aggregate summary, in display order.
_BUCKET_ORDER = ("0", "1", "2", "3+", "unknown")

# A title shorter than this is too generic to trust as a transcript-search substring (e.g. "fix
# it") - using it as a needle would flood token totals with unrelated sessions. Longer titles are
# specific enough that a verbatim substring match is a real signal, not noise. 12, not 6 (review
# finding): "fix it" is exactly 6 chars, so a 6-char floor admitted the comment's own
# counterexample.
_MIN_TITLE_NEEDLE_LEN = 12


class StatsError(Exception):
    """A ticket reference or --repo could not be resolved to `owner/repo` - caught by `run()`
    and turned into a clean `error: ...` line + nonzero exit, never a traceback."""


@dataclass
class TicketStats:
    """One product ticket's observed review/follow-up/token facts - every "unavailable" case
    carries its own reason string, distinct from a genuine zero/empty result."""

    ticket_id: str
    title: str
    state: str
    review_iterations: int | None
    review_iterations_reason: str | None
    followups: list[dict]
    followups_unavailable_reason: str | None
    tokens: dict | None
    tokens_unavailable_reason: str | None

    def to_dict(self) -> dict:
        return {
            "ticket_id": self.ticket_id,
            "title": self.title,
            "state": self.state,
            "review_iterations": self.review_iterations,
            "review_iterations_reason": self.review_iterations_reason,
            "followups": self.followups,
            "followups_unavailable_reason": self.followups_unavailable_reason,
            "tokens": self.tokens,
            "tokens_unavailable_reason": self.tokens_unavailable_reason,
        }


# ── repo/ticket-reference resolution ─────────────────────────────────────────────────


def _parse_git_remote_url(url: str) -> str | None:
    """Parse `owner/repo` from an https or ssh git remote URL. Host-agnostic (this machine's
    `origin` may point at GitHub, a Forgejo host, or anything else - only the trailing two path
    segments matter)."""
    cleaned = url.strip()
    if cleaned.endswith(".git"):
        cleaned = cleaned[: -len(".git")]
    if cleaned.startswith("git@") and ":" in cleaned:
        path = cleaned.split(":", 1)[1]
    elif "://" in cleaned:
        after_scheme = cleaned.split("://", 1)[1]
        path = after_scheme.split("/", 1)[1] if "/" in after_scheme else ""
    else:
        return None
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        return None
    return f"{parts[-2]}/{parts[-1]}"


def _git_remote_repo(cwd: Path) -> str | None:
    """Best-effort `owner/repo` from `cwd`'s git origin remote, or None - never raises."""
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return _parse_git_remote_url(result.stdout.strip())


def resolve_ticket(ref: str, repo: str | None, cwd: Path | None = None) -> tuple[str, str]:
    """Resolve a ticket reference into `(owner/repo, bare numeric id)`.

    Accepts `owner/repo#N` (repo embedded in the ref) or a bare `N` (needs `--repo`, or a git
    origin remote in `cwd` that resolves to one). Raises `StatsError` with a human-readable
    reason on failure - never guesses a repo.
    """
    ref = ref.strip()
    if "#" in ref:
        repo_part, _, id_part = ref.partition("#")
        repo_part, id_part = repo_part.strip(), id_part.strip()
        if not _REPO_RE.match(repo_part) or not id_part.isdigit():
            raise StatsError(f"cannot parse ticket reference {ref!r} (expected owner/repo#N)")
        return repo_part, id_part

    if not ref.isdigit():
        raise StatsError(f"cannot parse ticket reference {ref!r} (expected owner/repo#N or N)")

    if repo:
        if not _REPO_RE.match(repo):
            raise StatsError(f"--repo must be 'owner/name', got {repo!r}")
        return repo, ref

    resolved = _git_remote_repo(cwd or Path.cwd())
    if resolved is None:
        raise StatsError(
            f"ticket reference {ref!r} has no repo - pass --repo owner/name or run inside a "
            "git repo whose origin remote resolves to owner/repo"
        )
    return resolved, ref


# ── subprocess -> JSON, shared by every `task`/`gh` call ────────────────────────────


def _run_json(cmd: list[str], timeout: float = 20.0) -> tuple[object | None, str | None]:
    """Run `cmd`, parse its stdout as JSON. Returns `(None, reason)` on ANY failure (missing
    binary, unreadable/no-exec binary, timeout, nonzero exit, malformed JSON) - never raises,
    never fabricates a result. `errors="replace"` on the text decode: adversarial/malformed
    stdout (e.g. a PR title with a byte sequence invalid for the locale) degrades to a
    replacement character instead of `subprocess.run` itself raising `UnicodeDecodeError`."""
    binary = cmd[0]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, errors="replace"
        )
    except FileNotFoundError:
        return None, f"`{binary}` not found on PATH"
    except subprocess.TimeoutExpired:
        return None, f"`{binary}` timed out after {timeout:g}s"
    except OSError as exc:
        # Broader than FileNotFoundError on purpose (review finding): a `PermissionError`
        # (binary exists but isn't executable) is a SIBLING of FileNotFoundError under OSError,
        # not a subclass of it, and previously escaped uncaught here.
        return None, f"`{binary}` could not be run: {exc}"
    if result.returncode != 0:
        stderr_lines = (result.stderr or "").strip().splitlines()
        detail = stderr_lines[-1] if stderr_lines else f"exit {result.returncode}"
        return None, f"`{' '.join(cmd)}` failed: {detail[:200]}"
    try:
        return json.loads(result.stdout), None
    except json.JSONDecodeError:
        return None, f"`{' '.join(cmd)}` returned invalid JSON"


# ── follow-up chain (task-cli) ───────────────────────────────────────────────────────


def fetch_ticket(ticket_id: str, repo: str) -> tuple[dict | None, str | None]:
    payload, reason = _run_json(["task", "read", ticket_id, "--repo", repo, "--json"])
    if payload is not None and not isinstance(payload, dict):
        return None, "`task read --json` returned a non-object payload"
    return payload, reason


def _followup_ids(links: object) -> list[str]:
    if not isinstance(links, dict):
        return []
    ids = [
        match.group(1)
        for key in links
        if isinstance(key, str) and (match := _FOLLOWED_UP_BY_RE.match(key))
    ]
    return sorted(ids, key=int)


def collect_followups(repo: str, links: object) -> tuple[list[dict], str | None]:
    """`([], None)` means "genuinely no follow-ups" - a lookup failure always carries a reason,
    distinguishing the two per this repo's fail-closed house rule.

    Assumes a follow-up lives in the SAME `repo` as the original ticket (task-cli's
    `follow-up-of`/`followed-up-by` link relation carries no cross-repo coordinate of its own -
    only a bare `#<id>` - so there is nothing else to resolve it against here). A cross-repo
    follow-up would 404 through `fetch_ticket` and surface as an ordinary lookup-failure reason,
    not a distinct "wrong repo" one - a known, undocumented-upstream limitation.
    """
    ids = _followup_ids(links)
    if not ids:
        return [], None
    followups: list[dict] = []
    errors: list[str] = []
    for followup_id in ids:
        payload, reason = fetch_ticket(followup_id, repo)
        if payload is None:
            errors.append(f"#{followup_id}: {reason}")
            continue
        followups.append(
            {
                "id": f"{repo}#{followup_id}",
                "title": str(payload.get("title") or ""),
                "state": str(payload.get("state") or ""),
            }
        )
    return followups, ("; ".join(errors) if errors else None)


# ── review iterations (gh) ───────────────────────────────────────────────────────────


def find_pr_numbers(ticket_id: str, repo: str) -> tuple[list[int] | None, str | None]:
    # Literal ticket id (not "#<id>") - matches this repo's assigned convention of searching
    # `<ticket-id> in:body`. Known limitations, both accepted here rather than adding a second,
    # unverified query heuristic: (1) false positive - an unrelated PR whose body happens to
    # mention the same bare number in a different context (another ticket, a line count, a port
    # number) can match; (2) false negative - GitHub's search index can under-index short/low
    # numeric tokens, so a real match can be missed, surfacing indistinguishably from "no PR
    # exists" as `review_iterations_reason`. `--all` mode avoids both via `list_repo_prs`'s
    # bulk fetch + local `\b<id>\b` matching instead of this search call.
    data, reason = _run_json(
        [
            "gh",
            "pr",
            "list",
            "--search",
            f"{ticket_id} in:body",
            "--repo",
            repo,
            "--json",
            "number",
        ]
    )
    if data is None:
        return None, reason
    if not isinstance(data, list):
        return None, "`gh pr list --json` returned a non-array payload"
    numbers = [
        item["number"]
        for item in data
        if isinstance(item, dict) and isinstance(item.get("number"), int)
    ]
    return numbers, None


def count_review_iterations(pr_numbers: list[int], repo: str) -> tuple[int | None, str | None]:
    """Count `CHANGES_REQUESTED` reviews across `pr_numbers` - a common, defensible proxy for
    "how many review rounds this ticket took", NOT an exact count. Known limitation: a PR with
    zero `CHANGES_REQUESTED` reviews but several commits pushed after an `APPROVED` review is
    undercounted by this proxy (a documented gap here, not silently hidden).

    When SOME (not all) matching PRs fail to load, `total` still reflects the PR(s) that DID
    load - it is real, just potentially incomplete - and the reason travels ALONGSIDE the count
    (not instead of it), so a partial result never presents as a confident, complete one (review
    finding: an earlier version silently dropped these errors once any PR succeeded)."""
    if not pr_numbers:
        return None, "no PR found referencing this ticket"
    total = 0
    errors: list[str] = []
    any_success = False
    for number in pr_numbers:
        data, reason = _run_json(
            ["gh", "pr", "view", str(number), "--repo", repo, "--json", "reviews"]
        )
        if data is None or not isinstance(data, dict):
            errors.append(f"PR #{number}: {reason or 'non-object payload'}")
            continue
        any_success = True
        reviews = data.get("reviews") or []
        total += sum(
            1 for r in reviews if isinstance(r, dict) and r.get("state") == "CHANGES_REQUESTED"
        )
    if not any_success:
        return None, "; ".join(errors)
    if errors:
        return total, f"partial - some PRs uncounted: {'; '.join(errors)}"
    return total, None


# ── token usage (local transcript search) ────────────────────────────────────────────


def _normalized_title_needle(title: str) -> str | None:
    normalized = " ".join(title.strip().lower().split())
    return normalized if len(normalized) >= _MIN_TITLE_NEEDLE_LEN else None


def _parse_transcript_cached(path: Path, cache: dict, lock: threading.Lock):
    """`transcripts.parse_transcript(path)`, memoized in `cache` (keyed by path) and guarded by
    `lock` - the SAME transcript file is often matched by more than one needle for one ticket,
    and by DIFFERENT tickets in an `--all` run (review finding: without this, a large transcript
    could be re-read and re-parsed hundreds of times in one `--all` invocation). `cache`/`lock`
    are owned by the caller (a fresh dict+lock per single-ticket call, one SHARED pair across an
    entire `--all` run) so the memoization scope is explicit, never a hidden module global."""
    from harness_cli import transcripts

    with lock:
        if path in cache:
            return cache[path]
    try:
        session = transcripts.parse_transcript(path, account="stats")
    except OSError:
        session = None
    with lock:
        cache.setdefault(path, session)
    return cache[path]


def collect_token_usage(
    ticket_id: str,
    title: str,
    transcript_cache: dict,
    cache_lock: threading.Lock,
    corpus: dict,
) -> tuple[dict | None, str | None]:
    # `corpus` (harness_cli.search.build_transcript_corpus) is a full one-time scan of the
    # local transcript corpus, built ONCE by the caller and reused across every ticket/needle
    # (review finding, three separate rounds: calling `search_user_messages` per needle per
    # ticket rescanned the whole corpus from disk every time). `paths_matching_id` is an exact
    # `"#<id>"` token-set lookup (review finding: a raw substring check let ticket "#2" match a
    # file that only ever mentions "#21"/"#200" - never fabricate a match on a digit prefix).
    from harness_cli.search import paths_matching_id, paths_matching_text

    paths: set[Path] = set(paths_matching_id(corpus, ticket_id))
    title_needle = _normalized_title_needle(title)
    if title_needle is not None:
        paths.update(paths_matching_text(corpus, title_needle))
    if not paths:
        return None, "no local transcript match"

    sessions = [
        s
        for s in (_parse_transcript_cached(path, transcript_cache, cache_lock) for path in paths)
        if s is not None
    ]
    if not sessions:
        return None, "no local transcript match"

    total = sum(s.totals.total_tokens for s in sessions)
    return {"total": total, "sessions": len(sessions)}, None


# ── per-ticket assembly ───────────────────────────────────────────────────────────────


def build_ticket_stats(
    ticket_id: str,
    repo: str,
    *,
    transcript_cache: dict | None = None,
    cache_lock: threading.Lock | None = None,
    prs: list[dict] | None = None,
    corpus: dict[Path, str] | None = None,
) -> TicketStats:
    """Build one ticket's stats. `transcript_cache`/`cache_lock`/`corpus` are an optional
    SHARED trio a caller iterating many tickets (`build_all_stats`) passes in so the transcript
    scan/parse below happen ONCE for the whole run; a single-ticket caller gets private,
    per-call ones.

    `prs`, when given, is a pre-fetched `list_repo_prs(repo)` result (with each PR's `reviews`
    already attached) - PR discovery AND review counting become a local, subprocess-free match
    (`_prs_matching`/`_count_changes_requested`) instead of this ticket's own
    `gh pr list --search` + `gh pr view` calls (`--all` passes this so N tickets share ONE bulk
    fetch rather than firing 2N calls at GitHub - review finding, three rounds)."""
    if transcript_cache is None:
        transcript_cache = {}
    if cache_lock is None:
        cache_lock = threading.Lock()
    if corpus is None:
        from harness_cli.search import build_transcript_corpus

        corpus = build_transcript_corpus()

    full_id = f"{repo}#{ticket_id}"
    payload, reason = fetch_ticket(ticket_id, repo)
    if payload is None:
        unavailable = f"ticket lookup failed: {reason}"
        return TicketStats(
            ticket_id=full_id,
            title="",
            state="",
            review_iterations=None,
            review_iterations_reason=unavailable,
            followups=[],
            followups_unavailable_reason=unavailable,
            tokens=None,
            tokens_unavailable_reason=unavailable,
        )

    title = str(payload.get("title") or "")
    state = str(payload.get("state") or "")
    followups, followups_reason = collect_followups(repo, payload.get("links"))

    if prs is not None:
        matching_prs = _prs_matching(prs, ticket_id)
        if not matching_prs:
            review_iterations, review_reason = None, "no PR found referencing this ticket"
        else:
            review_iterations, review_reason = _count_changes_requested(matching_prs), None
    else:
        pr_numbers, pr_reason = find_pr_numbers(ticket_id, repo)
        if pr_numbers is None:
            review_iterations, review_reason = None, pr_reason
        else:
            review_iterations, review_reason = count_review_iterations(pr_numbers, repo)

    tokens, tokens_reason = collect_token_usage(
        ticket_id, title, transcript_cache, cache_lock, corpus
    )

    return TicketStats(
        ticket_id=full_id,
        title=title,
        state=state,
        review_iterations=review_iterations,
        review_iterations_reason=review_reason,
        followups=followups,
        followups_unavailable_reason=followups_reason,
        tokens=tokens,
        tokens_unavailable_reason=tokens_reason,
    )


# ── --all mode: every closed ticket in a repo + an aggregate summary ────────────────

# `gh issue list --limit` silently caps its response with no signal that more exist - report the
# cap explicitly (below) rather than let `render_all`'s "N closed ticket(s)" imply completeness
# when a repo may have more (review finding).
_CLOSED_ISSUES_LIMIT = 200

# Bounded fan-out for `--all`: each ticket's `task read`/`gh pr view` calls are independent,
# read-only I/O - run them concurrently instead of hundreds of calls strictly in series (review
# finding: a repo with `_CLOSED_ISSUES_LIMIT` closed tickets could otherwise take minutes-to-
# tens-of-minutes of serial subprocess latency).
_ALL_MODE_MAX_WORKERS = 8

# `gh pr list`'s own cap for the bulk `--all`-mode PR fetch below - see `list_repo_prs`.
_ALL_PRS_LIMIT = 1000


# `gh pr list --state all --json number,body,reviews` fetches EVERY PR's review list in one
# call - real repos with a few hundred PRs routinely exceed `_run_json`'s default 20s timeout
# for this specific call (review finding), where the plain-JSON `gh issue list`/`gh pr view`
# calls elsewhere in this module comfortably fit inside it.
_BULK_PR_FETCH_TIMEOUT = 90.0


def list_repo_prs(repo: str) -> tuple[list[dict] | None, str | None, bool]:
    """Every PR in `repo` (any state) with its reviews, fetched ONCE for `--all` mode instead
    of each ticket making its own `gh pr list --search` + `gh pr view` calls (review finding,
    three rounds: N tickets x their own search call hits GitHub's tightly rate-limited search
    endpoint, and a SEPARATE `gh pr view` per matching PR multiplies that further - a repo with
    more than a couple dozen closed tickets would see most calls come back rate-limited under
    `--all`'s concurrent fan-out, silently landing those tickets in the "unknown" review-
    iteration bucket). `number`/`body`/`reviews` in ONE call; matched locally per ticket by
    `_prs_matching` (a `\\b<id>\\b` word-boundary match - NOT a raw substring, which would let
    ticket "2" match a PR body containing "42"/"21"/a port number/etc., a real review finding)
    and counted locally by `_count_changes_requested` - no further subprocess needed.

    Third element is `True` when `_ALL_PRS_LIMIT` was hit - mirrors `list_closed_ticket_ids`'s
    own truncation signal (review finding: an earlier version tracked this for closed issues
    but not for PRs, so a ticket whose real matching PR fell past the cap silently read as "no
    PR found" with no hint the fetch itself was incomplete)."""
    data, reason = _run_json(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "all",
            "--json",
            "number,body,reviews",
            "--limit",
            str(_ALL_PRS_LIMIT),
        ],
        timeout=_BULK_PR_FETCH_TIMEOUT,
    )
    if data is None:
        return None, reason, False
    if not isinstance(data, list):
        return None, "`gh pr list --json` returned a non-array payload", False
    prs = [item for item in data if isinstance(item, dict)]
    return prs, None, len(prs) >= _ALL_PRS_LIMIT


def _prs_matching(prs: list[dict], ticket_id: str) -> list[dict]:
    # Anchored to "#<id>" (task-cli's own "Refs #N" convention, per this module's own docstring
    # above), not a bare `\b<id>\b` - a bare-number word-boundary still matched any incidental
    # digit in a PR body ("retry count to 2"), not just a real ticket reference (review finding).
    pattern = re.compile(rf"#{re.escape(ticket_id)}\b")
    return [
        pr
        for pr in prs
        if isinstance(pr.get("number"), int) and pattern.search(pr.get("body") or "")
    ]


def _count_changes_requested(prs: list[dict]) -> int:
    total = 0
    for pr in prs:
        reviews = pr.get("reviews") or []
        total += sum(
            1 for r in reviews if isinstance(r, dict) and r.get("state") == "CHANGES_REQUESTED"
        )
    return total


def list_closed_ticket_ids(repo: str) -> tuple[list[str] | None, str | None, bool]:
    data, reason = _run_json(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "closed",
            "--json",
            "number,title,state",
            "--limit",
            str(_CLOSED_ISSUES_LIMIT),
        ]
    )
    if data is None:
        return None, reason, False
    if not isinstance(data, list):
        return None, "`gh issue list --json` returned a non-array payload", False
    ids = [
        str(item["number"])
        for item in data
        if isinstance(item, dict) and isinstance(item.get("number"), int)
    ]
    return ids, None, len(ids) >= _CLOSED_ISSUES_LIMIT


def _bucket(value: int | None) -> str:
    if value is None:
        return "unknown"
    if value <= 0:
        return "0"
    if value == 1:
        return "1"
    if value == 2:
        return "2"
    return "3+"


def summarize(results: list[TicketStats]) -> dict:
    buckets = {label: 0 for label in _BUCKET_ORDER}
    with_followups = 0
    with_tokens = 0
    for result in results:
        buckets[_bucket(result.review_iterations)] += 1
        if result.followups:
            with_followups += 1
        if result.tokens is not None:
            with_tokens += 1
    return {
        "total_tickets": len(results),
        "review_iteration_buckets": buckets,
        "with_followups": with_followups,
        "with_tokens": with_tokens,
    }


def build_all_stats(repo: str) -> tuple[list[TicketStats], dict, str | None]:
    ids, reason, truncated = list_closed_ticket_ids(repo)
    if ids is None:
        return [], summarize([]), reason

    # Best-effort: a failed bulk fetch (missing gh, rate-limited, network error) falls back to
    # each ticket making its own `gh pr list --search` call inside `build_ticket_stats` below
    # (`prs=None`) rather than failing the whole `--all` run over review-iteration data alone -
    # slower and reintroduces the N+1 search-endpoint risk `list_repo_prs` exists to avoid, but
    # still correct, and follow-up/token data are unaffected either way.
    prs, _prs_reason, prs_truncated = list_repo_prs(repo)

    # Built ONCE for the whole run, not per ticket (review finding, three rounds) - see
    # `collect_token_usage`'s docstring.
    from harness_cli.search import build_transcript_corpus

    corpus = build_transcript_corpus()

    transcript_cache: dict = {}
    cache_lock = threading.Lock()
    if ids:
        with ThreadPoolExecutor(max_workers=min(_ALL_MODE_MAX_WORKERS, len(ids))) as pool:
            results = list(
                pool.map(
                    lambda ticket_id: build_ticket_stats(
                        ticket_id,
                        repo,
                        transcript_cache=transcript_cache,
                        cache_lock=cache_lock,
                        prs=prs,
                        corpus=corpus,
                    ),
                    ids,
                )
            )
    else:
        results = []

    summary = summarize(results)
    summary["truncated"] = truncated
    summary["prs_truncated"] = prs_truncated if prs is not None else False
    return results, summary, None


# ── rendering ─────────────────────────────────────────────────────────────────────────


def stats_to_json(stats: TicketStats) -> str:
    return json.dumps(stats.to_dict(), indent=2)


def all_to_json(results: list[TicketStats], summary: dict) -> str:
    return json.dumps({"tickets": [r.to_dict() for r in results], "summary": summary}, indent=2)


def render_stats(stats: TicketStats) -> str:
    lines = [f"{_sanitize(stats.ticket_id)}  {_sanitize(stats.title)}  [{_sanitize(stats.state)}]"]
    if stats.review_iterations is None:
        lines.append(
            f"  review iterations: unknown ({_sanitize(stats.review_iterations_reason or '')})"
        )
    elif stats.review_iterations_reason:
        lines.append(
            f"  review iterations: {stats.review_iterations} "
            f"({_sanitize(stats.review_iterations_reason)})"
        )
    else:
        lines.append(f"  review iterations: {stats.review_iterations}")

    if stats.followups:
        lines.append(f"  follow-ups: {len(stats.followups)}")
        for followup in stats.followups:
            lines.append(
                f"    - {_sanitize(followup['id'])}  {_sanitize(followup['title'])}  "
                f"[{_sanitize(followup['state'])}]"
            )
    elif stats.followups_unavailable_reason:
        lines.append(f"  follow-ups: unknown ({_sanitize(stats.followups_unavailable_reason)})")
    else:
        lines.append("  follow-ups: none")

    if stats.tokens is None:
        lines.append(f"  tokens: unknown ({_sanitize(stats.tokens_unavailable_reason or '')})")
    else:
        lines.append(
            f"  tokens: {stats.tokens['total']:,} across {stats.tokens['sessions']} session(s)"
        )
    return "\n".join(lines)


def render_all(results: list[TicketStats], summary: dict) -> str:
    if not results:
        return "No closed tickets found."
    lines = [f"{summary['total_tickets']} closed ticket(s):"]
    if summary.get("truncated"):
        lines.append(
            f"  (gh issue list capped at {_CLOSED_ISSUES_LIMIT} - this repo may have more "
            "closed tickets)"
        )
    if summary.get("prs_truncated"):
        lines.append(
            f"  (gh pr list capped at {_ALL_PRS_LIMIT} - some review-iteration counts may be "
            "incomplete)"
        )
    lines.append("")
    for result in results:
        lines.append(render_stats(result))
        lines.append("")
    buckets = summary["review_iteration_buckets"]
    bucket_line = ", ".join(f"{label}={buckets[label]}" for label in _BUCKET_ORDER)
    lines.append("Summary:")
    lines.append(f"  review iteration buckets: {bucket_line}")
    lines.append(f"  with at least one follow-up: {summary['with_followups']}")
    lines.append(f"  with token data available: {summary['with_tokens']}")
    return "\n".join(lines)


# ── CLI entry point ──────────────────────────────────────────────────────────────────


def run(args) -> int:
    repo_flag = getattr(args, "repo", None)
    all_mode = bool(getattr(args, "all", False))
    json_mode = bool(getattr(args, "json", False))
    ticket_ref = getattr(args, "ticket_ref", None)

    if all_mode:
        if ticket_ref:
            print("error: TICKET and --all are mutually exclusive", file=sys.stderr)
            return 2
        repo = repo_flag or _git_remote_repo(Path.cwd())
        if repo is None:
            print(
                "error: --all needs --repo owner/name or a git origin remote resolving to one",
                file=sys.stderr,
            )
            return 2
        if not _REPO_RE.match(repo):
            print(f"error: --repo must be 'owner/name', got {repo!r}", file=sys.stderr)
            return 2
        results, summary, reason = build_all_stats(repo)
        if reason is not None:
            print(f"error: {reason}", file=sys.stderr)
            return 1
        print(all_to_json(results, summary) if json_mode else render_all(results, summary))
        return 0

    if not ticket_ref:
        print("error: TICKET is required unless --all is given", file=sys.stderr)
        return 2

    try:
        repo, ticket_id = resolve_ticket(ticket_ref, repo_flag)
    except StatsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    stats = build_ticket_stats(ticket_id, repo)
    print(stats_to_json(stats) if json_mode else render_stats(stats))
    return 0
