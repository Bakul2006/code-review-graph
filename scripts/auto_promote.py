#!/usr/bin/env python3
"""Decide whether `staging` may be promoted to `testing`, and render the PR.

``.github/workflows/auto-promote.yml`` fetches four payloads with ``git`` and
``gh``, hands them to ``decide``, and then does exactly what the verdict says.
Every judgement lives here so it can be driven from a test with no network:
the workflow contains no rule of its own.

The rule, in the maintainer's words: *once a day, when staging is green and
has something testing does not*. Three verdicts come out of it:

``READY``
    Open or update the promotion pull request and merge it with a merge
    commit.
``NOT_READY``
    A state the repository reaches by itself and gets out of by itself:
    nothing to promote, CI still running, mergeability not computed yet.
    Tomorrow's run will look again. Not a failure.
``BLOCKED``
    A state that needs a person: a required check failed, a required context
    never reported at all, the open promotion pull request conflicts or is
    held by a review requirement this token cannot satisfy. Also not a red
    run -- see "Why nothing here fails the run" below.

Design notes worth keeping:

* **The required contexts are read from the ruleset at run time**, through
  ``GET /repos/{owner}/{repo}/rules/branches/testing``, not hardcoded. That
  endpoint needs only read access to the repository, unlike the
  ``/rulesets/{id}`` one, so the workflow can stay on a read-scoped token for
  the decision. Hardcoding the eight strings would mean a renamed CI job
  silently drops out of the gate.
* **A required context that was skipped or neutral is not green here**, even
  though GitHub's own required-status-check evaluation accepts both. This
  gate is deliberately stricter than the ruleset: it may refuse a promotion
  GitHub would have allowed, and it can never allow one GitHub would refuse.
  A job that skipped tested nothing, and an unattended daily merge is the
  wrong place to be generous. ``scripts/promotion_gate.py`` takes the same
  line about skipped checks for the same reason.
* **Why nothing here fails the run.** Every verdict this module produces
  describes the repository, not the automation, and every one of them is
  already visible elsewhere: a red required check is a red CI run on
  `staging`, a conflict is visible on the pull request. A red run per day for
  a state the maintainer already knows about trains him to ignore the daily
  mail. The one outcome that *is* a failure is the automation asking GitHub
  to merge and being told no -- that is the automation's own assumption about
  the repository turning out to be wrong, nothing else reports it, and the
  pull request would otherwise sit open forever. The workflow raises that
  one, and only that one, as a red run.

This script can never target ``main``. The branches are the two constants
below, no argument, environment variable or payload field can change them,
and ``assert_safe_targets`` refuses anything else.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# The only promotion this script performs. Promotion to `main` is a manual
# step and stays one: see CONTRIBUTING.md "Branching and promotion".
HEAD_BRANCH = "staging"
BASE_BRANCH = "testing"

# Branches no automated merge may ever write to.
NEVER_AUTOMATED = ("main", "master")

# Verdicts.
READY = "READY"
NOT_READY = "NOT_READY"
BLOCKED = "BLOCKED"

# Per-context outcomes.
GREEN = "green"
RED = "red"
PENDING = "pending"
ABSENT = "absent"
NOT_RUN = "not-run"

_GREEN_CONCLUSIONS = frozenset({"success"})
# "skipped" and "neutral" are treated as passing by GitHub's required status
# checks. They are not treated as passing here; see the module docstring.
_NOT_RUN_CONCLUSIONS = frozenset({"skipped", "neutral"})

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_PR_REF = re.compile(r"#(\d+)")
_MAX_LINE = 240
_MAX_COMMITS_SHOWN = 200

# gh and the REST API spell mergeability differently; both are accepted.
_CONFLICTING = {"false", "conflicting", "dirty"}
_UNKNOWN = {"none", "null", "unknown", ""}


class UnsafeTargetError(Exception):
    """A branch this script is not allowed to touch was asked for."""


def assert_safe_targets(head: str = HEAD_BRANCH, base: str = BASE_BRANCH) -> None:
    """Refuse any promotion other than `staging` -> `testing`.

    The constants above are the only values the command line can produce, so
    this cannot fire in normal use. It exists so that a future edit which
    threads a branch through from somewhere else fails loudly and in a test,
    rather than quietly merging something into `main` at 06:00.
    """
    for name, role in ((head, "head"), (base, "base")):
        if name in NEVER_AUTOMATED:
            raise UnsafeTargetError(
                f"{name!r} was given as the {role} branch. Promotion to "
                f"{name!r} is never automatic; the maintainer opens and merges "
                "that pull request by hand."
            )
    if (head, base) != (HEAD_BRANCH, BASE_BRANCH):
        raise UnsafeTargetError(
            f"this workflow promotes {HEAD_BRANCH!r} to {BASE_BRANCH!r} only, "
            f"not {head!r} to {base!r}."
        )


def clean(text: object) -> str:
    """Strip control characters and clamp one line for display."""
    flat = _CONTROL_CHARS.sub("", str(text)).strip()
    if len(flat) > _MAX_LINE:
        flat = flat[: _MAX_LINE - 3] + "..."
    return flat


# ---------------------------------------------------------------------------
# reading the payloads
# ---------------------------------------------------------------------------


def required_contexts(rules: Any) -> tuple[str, ...]:
    """Pull the required status check contexts out of the branch rules.

    ``rules`` is the body of ``GET /repos/{owner}/{repo}/rules/branches/{branch}``:
    a flat list of the rules that apply, whichever ruleset they came from.
    An empty result means the caller could not establish what has to be green,
    which is a refusal, not a pass.
    """
    found: list[str] = []
    for rule in rules if isinstance(rules, list) else []:
        if not isinstance(rule, dict) or rule.get("type") != "required_status_checks":
            continue
        parameters = rule.get("parameters") or {}
        for entry in parameters.get("required_status_checks") or []:
            context = (entry or {}).get("context") if isinstance(entry, dict) else None
            if isinstance(context, str) and context.strip():
                found.append(context.strip())
    seen: set[str] = set()
    ordered: list[str] = []
    for context in found:
        if context not in seen:
            seen.add(context)
            ordered.append(context)
    return tuple(ordered)


def normalise_reports(payload: Any) -> list[dict[str, Any]]:
    """Flatten check runs and commit statuses into one shape.

    A required context can be satisfied by either, and a repository that
    later adds a third-party status check should not need this script
    changed. Each entry comes back as ``{name, status, conclusion, when}``.
    """
    reports: list[dict[str, Any]] = []
    runs: Any = []
    statuses: Any = []
    if isinstance(payload, dict):
        runs = payload.get("check_runs") or []
        statuses = payload.get("statuses") or []
    elif isinstance(payload, list):
        runs = payload
    for run in runs if isinstance(runs, list) else []:
        if not isinstance(run, dict):
            continue
        name = run.get("name")
        if not isinstance(name, str):
            continue
        reports.append(
            {
                "name": name,
                "status": str(run.get("status") or ""),
                "conclusion": run.get("conclusion"),
                "when": str(run.get("completed_at") or run.get("started_at") or ""),
            }
        )
    for status in statuses if isinstance(statuses, list) else []:
        if not isinstance(status, dict):
            continue
        context = status.get("context")
        if not isinstance(context, str):
            continue
        state = str(status.get("state") or "").lower()
        reports.append(
            {
                "name": context,
                "status": "completed" if state != "pending" else "in_progress",
                "conclusion": {"success": "success", "pending": None}.get(state, "failure"),
                "when": str(status.get("updated_at") or status.get("created_at") or ""),
            }
        )
    return reports


def latest_by_name(reports: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Keep only the most recent report per context name.

    A re-run leaves the old check run on the commit, still carrying its old
    conclusion. Taking the newest by timestamp is what makes "I re-ran the
    flaky job and it is green now" work; taking the first would promote on
    stale evidence or refuse on a failure that has already been fixed.
    """
    newest: dict[str, dict[str, Any]] = {}
    for report in reports:
        name = report["name"]
        current = newest.get(name)
        if current is None or str(report.get("when") or "") >= str(current.get("when") or ""):
            newest[name] = report
    return newest


@dataclass(frozen=True)
class ContextState:
    """One required context and what the head commit says about it."""

    context: str
    state: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"context": self.context, "state": self.state, "detail": self.detail}


def classify(required: tuple[str, ...], reports: list[dict[str, Any]]) -> tuple[ContextState, ...]:
    """Say, for each required context, whether the head commit clears it."""
    newest = latest_by_name(reports)
    out: list[ContextState] = []
    for context in required:
        report = newest.get(context)
        if report is None:
            out.append(
                ContextState(
                    context,
                    ABSENT,
                    "no check run or status with this name reported on the commit",
                )
            )
            continue
        status = str(report.get("status") or "").lower()
        conclusion = str(report.get("conclusion") or "").lower()
        if status != "completed":
            out.append(ContextState(context, PENDING, f"still {status or 'queued'}"))
        elif conclusion in _GREEN_CONCLUSIONS:
            out.append(ContextState(context, GREEN, "success"))
        elif conclusion in _NOT_RUN_CONCLUSIONS:
            out.append(
                ContextState(
                    context,
                    NOT_RUN,
                    f"{conclusion}: the job did not run, so it verified nothing",
                )
            )
        else:
            out.append(ContextState(context, RED, conclusion or "completed with no conclusion"))
    return tuple(out)


def _mergeability(pull_request: dict[str, Any]) -> tuple[str, str]:
    """Normalise gh's and the REST API's two spellings of mergeability."""
    raw = pull_request.get("mergeable")
    mergeable = str(raw).strip().lower() if raw is not None else "none"
    state = str(pull_request.get("mergeStateStatus") or pull_request.get("mergeable_state") or "")
    return mergeable, state.strip().upper()


def open_promotion_pr(payload: Any) -> dict[str, Any] | None:
    """Return the open `staging` -> `testing` pull request, if there is one.

    ``gh pr list --json`` always yields an array; the first entry is used,
    because two open pull requests with the same head and base cannot exist.
    """
    entries = payload if isinstance(payload, list) else (payload or {}).get("items") or []
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and entry.get("number"):
            return entry
    return None


def parse_commits(text: str) -> list[str]:
    """One promoted commit per line, as `git log --format` wrote them."""
    return [clean(line) for line in (text or "").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# the decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    """The verdict, everything it was based on, and why."""

    state: str
    reason: str
    contexts: tuple[ContextState, ...] = ()
    commits: int = 0
    pr_number: int | None = None
    notes: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return self.state == READY

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "head": HEAD_BRANCH,
            "base": BASE_BRANCH,
            "commits": self.commits,
            "pr_number": self.pr_number,
            "contexts": [context.as_dict() for context in self.contexts],
            "notes": list(self.notes),
        }


def decide(
    *,
    commits: int,
    rules: Any,
    reports: list[dict[str, Any]],
    pull_request: dict[str, Any] | None,
    head_sha: str = "",
    base_sha: str = "",
) -> Decision:
    """Decide whether `staging` may be promoted to `testing` right now.

    The order of the guards is the order in which an answer is most useful to
    read. A failed required check is reported as a failed required check, not
    as "the pull request is blocked", even though the second is what GitHub
    would say about the same situation.
    """
    assert_safe_targets()

    if head_sha and base_sha and head_sha == base_sha:
        return Decision(NOT_READY, f"`{HEAD_BRANCH}` and `{BASE_BRANCH}` are the same commit.")
    if commits <= 0:
        return Decision(
            NOT_READY,
            f"`{HEAD_BRANCH}` has no commits that `{BASE_BRANCH}` lacks.",
        )

    required = required_contexts(rules)
    if not required:
        return Decision(
            BLOCKED,
            f"no required status check could be read for `{BASE_BRANCH}`, so there is "
            "nothing to verify staging against. Promoting blind is not an option.",
            commits=commits,
            notes=(
                "The contexts are read from GET /repos/OWNER/REPO/rules/branches/"
                f"{BASE_BRANCH}. An empty answer means the ruleset changed, or the "
                "token could not read it.",
            ),
        )

    contexts = classify(required, reports)
    red = [item for item in contexts if item.state == RED]
    not_run = [item for item in contexts if item.state == NOT_RUN]
    pending = [item for item in contexts if item.state == PENDING]
    absent = [item for item in contexts if item.state == ABSENT]

    if red:
        return Decision(
            BLOCKED,
            "required check(s) did not pass on the `{}` tip: {}.".format(
                HEAD_BRANCH, ", ".join(f"`{item.context}` ({item.detail})" for item in red)
            ),
            contexts=contexts,
            commits=commits,
        )
    if not_run:
        return Decision(
            BLOCKED,
            "required check(s) were skipped rather than run on the `{}` tip: {}.".format(
                HEAD_BRANCH, ", ".join(f"`{item.context}`" for item in not_run)
            ),
            contexts=contexts,
            commits=commits,
            notes=(
                "GitHub counts a skipped required check as a pass. This workflow "
                "does not: a job that did not run verified nothing. Promote by hand "
                "if the skip was deliberate.",
            ),
        )
    if absent and pending:
        return Decision(
            NOT_READY,
            "CI has not finished on the `{}` tip: {} still running, {} not reported yet.".format(
                HEAD_BRANCH,
                ", ".join(f"`{item.context}`" for item in pending),
                ", ".join(f"`{item.context}`" for item in absent),
            ),
            contexts=contexts,
            commits=commits,
        )
    if absent:
        return Decision(
            BLOCKED,
            "required context(s) never reported on the `{}` tip and nothing is still "
            "running: {}.".format(
                HEAD_BRANCH, ", ".join(f"`{item.context}`" for item in absent)
            ),
            contexts=contexts,
            commits=commits,
            notes=(
                "A required context with no check run is usually a CI job that was "
                "renamed without the ruleset being updated, or a push-triggered run "
                "that never started. GitHub would refuse the merge too.",
            ),
        )
    if pending:
        return Decision(
            NOT_READY,
            "required check(s) are still running on the `{}` tip: {}.".format(
                HEAD_BRANCH, ", ".join(f"`{item.context}`" for item in pending)
            ),
            contexts=contexts,
            commits=commits,
        )

    number = None
    if pull_request is not None:
        raw_number = pull_request.get("number")
        number = int(raw_number) if isinstance(raw_number, (int, str)) else None
        if pull_request.get("isDraft") or pull_request.get("draft"):
            return Decision(
                BLOCKED,
                f"the open promotion pull request #{number} is a draft.",
                contexts=contexts,
                commits=commits,
                pr_number=number,
            )
        mergeable, merge_state = _mergeability(pull_request)
        if mergeable in _CONFLICTING or merge_state == "DIRTY":
            return Decision(
                BLOCKED,
                f"the open promotion pull request #{number} has conflicts with "
                f"`{BASE_BRANCH}` and cannot be merged automatically.",
                contexts=contexts,
                commits=commits,
                pr_number=number,
            )
        if merge_state == "BLOCKED":
            return Decision(
                BLOCKED,
                f"GitHub reports the open promotion pull request #{number} as blocked, "
                "so a rule is holding it that this token cannot satisfy.",
                contexts=contexts,
                commits=commits,
                pr_number=number,
                notes=(
                    "The likeliest cause is the `testing` ruleset's "
                    "require_extra_approval_for_unattributed_changes: the promoted "
                    "range contains a commit whose author does not map to a GitHub "
                    "account, so one approval is required, and GITHUB_TOKEN is not "
                    "allowed to approve pull requests. Approve and merge "
                    f"#{number} by hand.",
                ),
            )
        if mergeable in _UNKNOWN or merge_state in {"", "UNKNOWN"}:
            return Decision(
                NOT_READY,
                f"GitHub has not finished working out whether #{number} can be merged.",
                contexts=contexts,
                commits=commits,
                pr_number=number,
            )

    return Decision(
        READY,
        "`{}` is {} commit(s) ahead of `{}` and all {} required check(s) are green.".format(
            HEAD_BRANCH, commits, BASE_BRANCH, len(contexts)
        ),
        contexts=contexts,
        commits=commits,
        pr_number=number,
    )


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def render_body(commits: list[str], run_url: str = "") -> str:
    """Render the promotion pull request body.

    Deliberately the same shape as the manual `Promote` workflow's body, so
    the two are indistinguishable in the pull request list and the maintainer
    reads one format, not two.
    """
    shown = commits[:_MAX_COMMITS_SHOWN]
    lines = [
        f"## Promote `{HEAD_BRANCH}` -> `{BASE_BRANCH}`",
        "",
        "Merge with a **merge commit** (not squash) so every author stays on their commits.",
        "",
        "### Included",
        "",
    ]
    lines.extend(f"- {line}" for line in shown)
    if len(commits) > len(shown):
        lines.append(f"- ... and {len(commits) - len(shown)} more commit(s).")
    lines.extend(["", "### Pull requests referenced", ""])
    numbers = sorted({int(match) for line in commits for match in _PR_REF.findall(line)})
    lines.extend(f"- #{number}" for number in numbers)
    lines.extend(
        [
            "",
            "---",
            "",
            "Opened automatically by `.github/workflows/auto-promote.yml`, which promotes "
            f"`{HEAD_BRANCH}` to `{BASE_BRANCH}` once a day when the required checks are "
            "green. Promotion to `main` is never automatic.",
        ]
    )
    if run_url:
        lines.append("")
        lines.append(f"[Run log]({clean(run_url)})")
    return "\n".join(lines) + "\n"


def _context_table(contexts: tuple[ContextState, ...]) -> list[str]:
    if not contexts:
        return []
    mark = {
        GREEN: "pass",
        RED: "FAIL",
        PENDING: "running",
        ABSENT: "DID NOT REPORT",
        NOT_RUN: "DID NOT RUN",
    }
    lines = ["| Required check | Result | Detail |", "| --- | --- | --- |"]
    lines.extend(
        f"| `{clean(item.context)}` | {mark.get(item.state, item.state)} | {clean(item.detail)} |"
        for item in contexts
    )
    return lines


def render_summary(decision: Decision, context: dict[str, str]) -> str:
    """Render the job summary. Written every run, including the quiet ones.

    A run that decided to do nothing has to say so in the same place as a run
    that promoted, or "the workflow did nothing" and "the workflow did not
    run" look identical from the Actions tab.
    """
    headline = {
        READY: f"**Promoting.** `{HEAD_BRANCH}` -> `{BASE_BRANCH}`.",
        NOT_READY: "**Nothing to do.**",
        BLOCKED: "**Held back.** A person needs to look at this.",
    }[decision.state]
    if context.get("dry_run") == "true" and decision.ready:
        headline = f"**Would promote** `{HEAD_BRANCH}` -> `{BASE_BRANCH}`, but this is a dry run."

    lines = ["## Auto promote", "", headline, "", decision.reason, ""]
    if decision.notes:
        lines.extend(f"> {clean(note)}" for note in decision.notes)
        lines.append("")
    lines.append(
        f"`{HEAD_BRANCH}` at `{clean(context.get('head_sha', ''))[:12]}`, "
        f"`{BASE_BRANCH}` at `{clean(context.get('base_sha', ''))[:12]}`, "
        f"{decision.commits} commit(s) in the range, "
        f"trigger {clean(context.get('event', '')) or 'unknown'}."
    )
    lines.append("")
    lines.extend(_context_table(decision.contexts))
    if decision.contexts:
        lines.append("")
    if decision.pr_number:
        lines.append(f"Promotion pull request: #{decision.pr_number}.")
        lines.append("")
    lines.append(
        f"Promotion of `{BASE_BRANCH}` to `main` is never automatic and this workflow "
        "cannot perform it."
    )
    return "\n".join(lines) + "\n"


def render_refusal(number: int, message: str, run_url: str = "") -> str:
    """Render the one outcome that is a red run: GitHub refused the merge."""
    lines = [
        "## Auto promote",
        "",
        f"**GitHub refused to merge the promotion pull request #{number}.** It is still "
        "open; nothing was merged and nothing was lost.",
        "",
        "What GitHub said:",
        "",
        "```",
    ]
    for raw in str(message).splitlines():
        body = clean(raw.replace("```", "'''"))
        if body:
            lines.append(body)
    lines.extend(
        [
            "```",
            "",
            "The usual cause is the `testing` ruleset's "
            "require_extra_approval_for_unattributed_changes: if any commit in the "
            "promoted range has an author that does not map to a GitHub account, one "
            "approving review is required, and GITHUB_TOKEN may not approve pull "
            f"requests. Approve and merge #{number} by hand, with a **merge commit**.",
            "",
            "This run is red on purpose. Every other outcome of this workflow is a state "
            "the repository reports elsewhere; this one is not reported anywhere but "
            "here.",
        ]
    )
    if run_url:
        lines.extend(["", f"[Run log]({clean(run_url)})"])
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------


def _load_json(path: str, default: Any) -> Any:
    if not path:
        return default
    file = Path(path)
    if not file.is_file():
        return default
    try:
        return json.loads(file.read_text(encoding="utf-8") or "null")
    except (OSError, json.JSONDecodeError):
        return default


def _write(path: str, text: str) -> None:
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _append_outputs(path: str, values: dict[str, str]) -> None:
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def cmd_decide(args: argparse.Namespace) -> int:
    commit_text = Path(args.commits).read_text(encoding="utf-8") if args.commits else ""
    commit_lines = parse_commits(commit_text)
    decision = decide(
        commits=len(commit_lines),
        rules=_load_json(args.rules, []),
        reports=normalise_reports(_load_json(args.checks, [])),
        pull_request=open_promotion_pr(_load_json(args.open_pr, [])),
        head_sha=args.head_sha,
        base_sha=args.base_sha,
    )
    context = {
        "head_sha": args.head_sha,
        "base_sha": args.base_sha,
        "event": args.event,
        "dry_run": "true" if args.dry_run else "false",
    }
    _write(args.out, json.dumps(decision.as_dict(), indent=2) + "\n")
    _write(args.body, render_body(commit_lines, args.run_url))
    summary = render_summary(decision, context)
    _write(args.summary, summary)
    _append_outputs(
        args.github_output,
        {
            "state": decision.state,
            "pr_number": str(decision.pr_number or ""),
            "commits": str(decision.commits),
        },
    )
    print(summary)
    level = "notice" if decision.state != BLOCKED else "warning"
    print(f"::{level} title=auto promote::{decision.state}: {clean(decision.reason)}")
    return 0


def cmd_refused(args: argparse.Namespace) -> int:
    message = Path(args.message).read_text(encoding="utf-8") if args.message else ""
    _write(args.summary, render_refusal(args.pr_number, message, args.run_url))
    print(render_refusal(args.pr_number, message, args.run_url))
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="daily staging -> testing promotion")
    sub = parser.add_subparsers(dest="command", required=True)

    # Note what is NOT here: no --head, no --base, no --branch. The branches
    # are constants, so no caller can point this at `main`.
    decider = sub.add_parser("decide", help="decide whether to promote, and render the PR")
    decider.add_argument("--rules", default="", help="GET /repos/O/R/rules/branches/testing")
    decider.add_argument("--checks", default="", help="GET /repos/O/R/commits/SHA/check-runs")
    decider.add_argument("--open-pr", default="", help="gh pr list --json output")
    decider.add_argument("--commits", default="", help="git log output, one commit per line")
    decider.add_argument("--head-sha", default="")
    decider.add_argument("--base-sha", default="")
    decider.add_argument("--event", default="")
    decider.add_argument("--run-url", default="")
    decider.add_argument("--dry-run", action="store_true")
    decider.add_argument("--out", default="", help="verdict JSON to write")
    decider.add_argument("--body", default="", help="pull request body to write")
    decider.add_argument("--summary", default="", help="job summary markdown to write")
    decider.add_argument("--github-output", default="", help="$GITHUB_OUTPUT to append to")

    refused = sub.add_parser("refused", help="report a merge GitHub refused; always exits 1")
    refused.add_argument("--pr-number", type=int, required=True)
    refused.add_argument("--message", default="", help="file holding what gh printed")
    refused.add_argument("--run-url", default="")
    refused.add_argument("--summary", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "decide":
        return cmd_decide(args)
    return cmd_refused(args)


if __name__ == "__main__":
    sys.exit(main())
