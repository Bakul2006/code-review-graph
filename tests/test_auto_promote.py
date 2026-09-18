"""Tests for the daily promotion: scripts/auto_promote.py and its workflow.

The workflow merges a pull request into a protected branch without a person
in the room, every day, so the decision that lets it do so has to be driven
from a test rather than trusted. Everything here is offline: the payloads are
the shapes GitHub really returns, recorded from this repository.

The most important test in the file is the last group. The workflow must
never be able to promote to the release branch, and that is asserted against
the script, against its command line and against the YAML, not merely
intended.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "auto_promote.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "auto-promote.yml"

_spec = importlib.util.spec_from_file_location("auto_promote", SCRIPT)
assert _spec is not None and _spec.loader is not None
promote = importlib.util.module_from_spec(_spec)
# @dataclass resolves annotations through sys.modules, so the module has to be
# registered before it is executed.
sys.modules["auto_promote"] = promote
_spec.loader.exec_module(promote)


# ---------------------------------------------------------------------------
# helpers: the payload shapes GitHub actually returns
# ---------------------------------------------------------------------------

REQUIRED = (
    "lint",
    "type-check",
    "security",
    "schema-sync",
    "test (3.10)",
    "test (3.11)",
    "test (3.12)",
    "test (3.13)",
)


def rules(contexts: tuple[str, ...] = REQUIRED) -> list[dict]:
    """The body of GET /repos/O/R/rules/branches/testing."""
    return [
        {"type": "deletion", "ruleset_id": 23449895},
        {"type": "non_fast_forward", "ruleset_id": 23449895},
        {
            "type": "pull_request",
            "parameters": {
                "required_approving_review_count": 0,
                "require_last_push_approval": False,
                "require_extra_approval_for_unattributed_changes": True,
                "allowed_merge_methods": ["merge"],
            },
            "ruleset_id": 23449895,
        },
        {
            "type": "required_status_checks",
            "parameters": {
                "strict_required_status_checks_policy": False,
                "required_status_checks": [{"context": name} for name in contexts],
            },
            "ruleset_id": 23449895,
        },
    ]


def run(name: str, conclusion: str | None = "success", status: str = "completed", when: str = "5"):
    return {"name": name, "status": status, "conclusion": conclusion, "completed_at": when}


def all_green(extra: list[dict] | None = None) -> list[dict]:
    runs = [run(name) for name in REQUIRED]
    runs.extend(extra or [])
    return promote.normalise_reports({"check_runs": runs})


def decide(**kwargs):
    base = {
        "commits": 12,
        "rules": rules(),
        "reports": all_green(),
        "pull_request": None,
        "head_sha": "a" * 40,
        "base_sha": "b" * 40,
    }
    base.update(kwargs)
    return promote.decide(**base)


# ---------------------------------------------------------------------------
# reading the payloads
# ---------------------------------------------------------------------------


def test_required_contexts_come_from_the_ruleset_not_from_a_hardcoded_list():
    assert promote.required_contexts(rules()) == REQUIRED
    # A renamed CI job changes the answer, which is the entire point.
    assert promote.required_contexts(rules(("lint", "test (3.14)"))) == ("lint", "test (3.14)")


def test_required_contexts_is_empty_when_there_is_no_such_rule():
    assert promote.required_contexts([{"type": "deletion"}]) == ()
    assert promote.required_contexts(None) == ()
    assert promote.required_contexts({"not": "a list"}) == ()


def test_commit_statuses_satisfy_a_required_context_too():
    reports = promote.normalise_reports(
        {"check_runs": [], "statuses": [{"context": "lint", "state": "success"}]}
    )
    assert promote.classify(("lint",), reports)[0].state == promote.GREEN


def test_a_rerun_supersedes_the_older_failing_run():
    # The failed run stays on the commit for ever. Reading the older one would
    # refuse to promote a branch somebody has already fixed.
    reports = promote.normalise_reports(
        {
            "check_runs": [
                run("lint", "failure", when="2026-09-18T10:00:00Z"),
                run("lint", "success", when="2026-09-18T12:00:00Z"),
            ]
        }
    )
    assert promote.classify(("lint",), reports)[0].state == promote.GREEN


# ---------------------------------------------------------------------------
# the decision
# ---------------------------------------------------------------------------


def test_ready_when_staging_is_ahead_and_every_required_check_is_green():
    decision = decide()
    assert decision.state == promote.READY
    assert decision.ready
    assert "8 required check(s) are green" in decision.reason
    assert {item.state for item in decision.contexts} == {promote.GREEN}


def test_a_check_that_is_green_but_not_required_cannot_hold_a_promotion():
    reports = all_green([run("Windows daemon and file handles", None, status="in_progress")])
    assert decide(reports=reports).state == promote.READY


def test_nothing_to_promote_is_not_ready_and_not_blocked():
    decision = decide(commits=0)
    assert decision.state == promote.NOT_READY
    assert "no commits that `testing` lacks" in decision.reason


def test_identical_tips_are_nothing_to_promote():
    assert decide(head_sha="c" * 40, base_sha="c" * 40).state == promote.NOT_READY


def test_unreadable_required_contexts_block_rather_than_promote_blind():
    decision = decide(rules=[{"type": "deletion"}])
    assert decision.state == promote.BLOCKED
    assert "nothing to verify staging against" in decision.reason


def test_a_failing_required_check_blocks_and_names_it():
    reports = promote.normalise_reports(
        {"check_runs": [run(name) for name in REQUIRED[:-1]] + [run(REQUIRED[-1], "failure")]}
    )
    decision = decide(reports=reports)
    assert decision.state == promote.BLOCKED
    assert "`test (3.13)` (failure)" in decision.reason


def test_a_pending_required_check_is_not_ready_because_tomorrow_fixes_it():
    reports = promote.normalise_reports(
        {
            "check_runs": [run(name) for name in REQUIRED[:-1]]
            + [run(REQUIRED[-1], None, status="in_progress")]
        }
    )
    decision = decide(reports=reports)
    assert decision.state == promote.NOT_READY
    assert "still running" in decision.reason


def test_a_required_context_absent_from_the_head_sha_blocks():
    # Nothing is still running, so waiting will not produce it: the job was
    # renamed, or its run never started. GitHub would refuse the merge too.
    reports = promote.normalise_reports({"check_runs": [run(name) for name in REQUIRED[:-1]]})
    decision = decide(reports=reports)
    assert decision.state == promote.BLOCKED
    assert "never reported" in decision.reason
    assert "`test (3.13)`" in decision.reason
    missing = [item for item in decision.contexts if item.state == promote.ABSENT]
    assert [item.context for item in missing] == ["test (3.13)"]


def test_an_absent_context_is_only_not_ready_while_something_is_still_running():
    reports = promote.normalise_reports(
        {
            "check_runs": [run(name) for name in REQUIRED[:-2]]
            + [run(REQUIRED[-2], None, status="queued")]
        }
    )
    decision = decide(reports=reports)
    assert decision.state == promote.NOT_READY
    assert "has not finished" in decision.reason


@pytest.mark.parametrize("conclusion", ["skipped", "neutral"])
def test_a_skipped_required_check_is_not_treated_as_a_pass(conclusion: str):
    # GitHub's own required-status-check evaluation accepts both. This gate is
    # deliberately stricter: a job that did not run verified nothing, and an
    # unattended daily merge is the wrong place to be generous.
    reports = promote.normalise_reports(
        {"check_runs": [run(name) for name in REQUIRED[:-1]] + [run(REQUIRED[-1], conclusion)]}
    )
    decision = decide(reports=reports)
    assert decision.state == promote.BLOCKED
    assert "skipped rather than run" in decision.reason


def test_a_failing_check_is_reported_before_a_blocked_pull_request():
    # GitHub would call this "the pull request is blocked". The failing check
    # is the useful sentence, so it wins.
    reports = promote.normalise_reports(
        {"check_runs": [run(name) for name in REQUIRED[:-1]] + [run(REQUIRED[-1], "failure")]}
    )
    decision = decide(
        reports=reports,
        pull_request={"number": 7, "mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"},
    )
    assert "test (3.13)" in decision.reason


# ---------------------------------------------------------------------------
# an already-open promotion pull request
# ---------------------------------------------------------------------------


def test_an_open_mergeable_pull_request_is_resumed_not_duplicated():
    decision = decide(
        pull_request={"number": 991, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"}
    )
    assert decision.state == promote.READY
    assert decision.pr_number == 991


def test_an_open_pull_request_with_conflicts_blocks():
    decision = decide(
        pull_request={"number": 991, "mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"}
    )
    assert decision.state == promote.BLOCKED
    assert "conflicts" in decision.reason
    assert decision.pr_number == 991


def test_a_pull_request_github_calls_blocked_explains_the_approval_the_bot_cannot_give():
    decision = decide(
        pull_request={"number": 991, "mergeable": "MERGEABLE", "mergeStateStatus": "BLOCKED"}
    )
    assert decision.state == promote.BLOCKED
    joined = " ".join(decision.notes)
    assert "require_extra_approval_for_unattributed_changes" in joined
    assert "not allowed to approve" in joined


def test_mergeability_not_yet_computed_is_not_ready():
    decision = decide(
        pull_request={"number": 991, "mergeable": None, "mergeStateStatus": "UNKNOWN"}
    )
    assert decision.state == promote.NOT_READY


def test_a_draft_promotion_pull_request_blocks():
    decision = decide(pull_request={"number": 991, "isDraft": True, "mergeable": "MERGEABLE"})
    assert decision.state == promote.BLOCKED
    assert "draft" in decision.reason


def test_the_rest_api_spelling_of_mergeability_is_understood_too():
    assert (
        decide(pull_request={"number": 5, "mergeable": False, "mergeable_state": "dirty"}).state
        == promote.BLOCKED
    )
    assert (
        decide(pull_request={"number": 5, "mergeable": True, "mergeable_state": "clean"}).state
        == promote.READY
    )


def test_open_promotion_pr_reads_the_gh_array():
    assert promote.open_promotion_pr([]) is None
    assert promote.open_promotion_pr([{"number": 3}]) == {"number": 3}


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def test_the_body_matches_the_manual_promote_workflow():
    manual = (REPO_ROOT / ".github" / "workflows" / "promote.yml").read_text(encoding="utf-8")
    body = promote.render_body(["abc1234 Fix a thing (#910) (Someone)"], run_url="https://x/1")
    assert body.startswith("## Promote `staging` -> `testing`")
    for heading in ("### Included", "### Pull requests referenced"):
        assert heading in body and heading in manual
    assert "Merge with a **merge commit** (not squash)" in body
    assert "- abc1234 Fix a thing (#910) (Someone)" in body
    assert "- #910" in body
    assert "Promotion to `main` is never automatic." in body


def test_the_body_sorts_pull_request_references_numerically_and_caps_the_list():
    body = promote.render_body([f"aaa{n} Subject (#{n}) (A)" for n in range(9, 260)])
    assert "... and 51 more commit(s)." in body
    numbers = [int(n) for n in re.findall(r"^- #(\d+)$", body, re.M)]
    assert numbers == sorted(numbers)
    assert numbers[:3] == [9, 10, 11]


def test_the_summary_is_written_even_when_nothing_happened():
    summary = promote.render_summary(decide(commits=0), {"event": "schedule"})
    assert "## Auto promote" in summary
    assert "**Nothing to do.**" in summary
    assert "never automatic" in summary


def test_the_summary_says_would_promote_on_a_dry_run():
    summary = promote.render_summary(decide(), {"event": "workflow_dispatch", "dry_run": "true"})
    assert "**Would promote**" in summary
    assert "| `lint` | pass |" in summary


def test_the_refusal_summary_names_the_pull_request_and_quotes_github():
    text = promote.render_refusal(991, "Pull request is not mergeable: ```oops")
    assert "#991" in text
    assert "Pull request is not mergeable" in text
    # Nothing quoted out of gh may close the fence early.
    assert "```oops" not in text
    assert "require_extra_approval_for_unattributed_changes" in text


# ---------------------------------------------------------------------------
# the command line
# ---------------------------------------------------------------------------


def test_decide_writes_the_verdict_the_body_the_summary_and_the_step_outputs(tmp_path: Path):
    (tmp_path / "rules.json").write_text(json.dumps(rules()), encoding="utf-8")
    (tmp_path / "checks.json").write_text(
        json.dumps({"check_runs": [run(name) for name in REQUIRED]}), encoding="utf-8"
    )
    (tmp_path / "pr.json").write_text("[]", encoding="utf-8")
    (tmp_path / "commits.txt").write_text("abc1234 Subject (#42) (A)\n", encoding="utf-8")
    outputs = tmp_path / "outputs.txt"

    code = promote.main(
        [
            "decide",
            "--rules", str(tmp_path / "rules.json"),
            "--checks", str(tmp_path / "checks.json"),
            "--open-pr", str(tmp_path / "pr.json"),
            "--commits", str(tmp_path / "commits.txt"),
            "--head-sha", "a" * 40,
            "--base-sha", "b" * 40,
            "--out", str(tmp_path / "verdict.json"),
            "--body", str(tmp_path / "body.md"),
            "--summary", str(tmp_path / "summary.md"),
            "--github-output", str(outputs),
        ]
    )
    assert code == 0
    verdict = json.loads((tmp_path / "verdict.json").read_text(encoding="utf-8"))
    assert verdict["state"] == promote.READY
    assert verdict["head"] == "staging" and verdict["base"] == "testing"
    assert verdict["commits"] == 1
    assert "- #42" in (tmp_path / "body.md").read_text(encoding="utf-8")
    assert "## Auto promote" in (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "state=READY" in outputs.read_text(encoding="utf-8")


def test_decide_survives_payload_files_that_are_missing_or_not_json(tmp_path: Path):
    # A gh call that failed must not crash the run into a stack trace; it has
    # to come out as a verdict a person can read.
    (tmp_path / "rules.json").write_text("not json", encoding="utf-8")
    (tmp_path / "commits.txt").write_text("abc1234 Subject (A)\n", encoding="utf-8")
    code = promote.main(
        [
            "decide",
            "--rules", str(tmp_path / "rules.json"),
            "--checks", str(tmp_path / "nope.json"),
            "--commits", str(tmp_path / "commits.txt"),
            "--out", str(tmp_path / "verdict.json"),
        ]
    )
    assert code == 0
    assert json.loads((tmp_path / "verdict.json").read_text())["state"] == promote.BLOCKED


def test_refused_always_exits_one(tmp_path: Path):
    (tmp_path / "log.txt").write_text("GraphQL: Pull Request is not mergeable", encoding="utf-8")
    code = promote.main(
        [
            "refused",
            "--pr-number", "991",
            "--message", str(tmp_path / "log.txt"),
            "--summary", str(tmp_path / "refusal.md"),
        ]
    )
    assert code == 1
    assert "not mergeable" in (tmp_path / "refusal.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# it can never target the release branch
# ---------------------------------------------------------------------------


def test_the_branches_are_constants_and_the_release_branch_is_refused():
    assert (promote.HEAD_BRANCH, promote.BASE_BRANCH) == ("staging", "testing")
    for head, base in (("testing", "main"), ("staging", "main"), ("main", "testing")):
        with pytest.raises(promote.UnsafeTargetError):
            promote.assert_safe_targets(head, base)
    promote.assert_safe_targets()


def test_the_command_line_offers_no_way_to_name_a_branch():
    parser = promote.build_parser()
    flags = {
        option
        for action in parser._subparsers._group_actions[0].choices["decide"]._actions
        for option in action.option_strings
    }
    assert not flags & {"--head", "--base", "--branch", "--target", "--into"}


def executable_yaml() -> str:
    """The workflow with its comment lines removed.

    Comments explain the rules; only the rest is what the runner does, and
    several of these assertions are about what it must never do.
    """
    return "\n".join(
        line
        for line in WORKFLOW.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


def test_the_workflow_never_names_the_release_branch_outside_a_comment():
    # Comments may explain that promotion to it is manual. Nothing the runner
    # executes may mention it at all.
    assert not re.search(r"\b(main|master)\b", executable_yaml())


def test_the_workflow_hardcodes_the_two_branches_and_takes_no_branch_input(workflow: dict):
    job = workflow["jobs"]["promote"]
    assert job["env"]["HEAD_BRANCH"] == "staging"
    assert job["env"]["BASE_BRANCH"] == "testing"
    inputs = (workflow.get("on") or workflow.get(True))["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"dry_run"}


# ---------------------------------------------------------------------------
# the workflow itself
# ---------------------------------------------------------------------------


@pytest.fixture
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_the_workflow_parses_and_has_no_dangling_needs(workflow: dict):
    jobs = workflow["jobs"]
    assert jobs, "the workflow defines no job"
    for name, job in jobs.items():
        needs = job.get("needs") or []
        needs = [needs] if isinstance(needs, str) else needs
        for dependency in needs:
            assert dependency in jobs, f"{name} needs {dependency}, which does not exist"


def test_it_runs_daily_and_by_hand_and_on_nothing_else(workflow: dict):
    triggers = workflow.get("on") or workflow.get(True)
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert len(triggers["schedule"]) == 1
    # A push trigger here would promote several times a day, and a
    # pull_request trigger would run it from an untrusted branch.
    assert "push" not in triggers and "pull_request" not in triggers


def test_a_hand_started_run_is_a_dry_run_unless_the_box_is_ticked(workflow: dict):
    dry_run = (workflow.get("on") or workflow.get(True))["workflow_dispatch"]["inputs"]["dry_run"]
    assert dry_run["type"] == "boolean"
    assert dry_run["default"] is True


def test_the_scheduled_run_is_not_a_dry_run(workflow: dict):
    # If it were, the workflow would never promote anything, which is the
    # whole feature. The expression is true only for a ticked manual run.
    expression = workflow["jobs"]["promote"]["env"]["DRY_RUN"]
    assert "workflow_dispatch" in expression and "inputs.dry_run" in expression


def test_two_runs_can_never_race(workflow: dict):
    concurrency = workflow["concurrency"]
    assert concurrency["group"]
    # Cancelling between "open the pull request" and "merge it" would leave a
    # pull request open with nobody reporting why.
    assert concurrency["cancel-in-progress"] is False


def test_permissions_are_read_at_the_top_and_only_widened_where_merging_needs_it(workflow: dict):
    assert workflow["permissions"] == {"contents": "read"}
    # contents: merge. pull-requests: open and edit. actions: restart the
    # checks the merge could not trigger. Nothing else.
    assert workflow["jobs"]["promote"]["permissions"] == {
        "contents": "write",
        "pull-requests": "write",
        "actions": "write",
    }


def test_the_merge_restarts_the_checks_a_github_token_push_cannot_trigger(workflow: dict):
    # A push made with GITHUB_TOKEN starts no `push` workflow run. Without
    # this the target tip would carry no required contexts, the next manual
    # promotion pull request would be unmergeable for ever, and the release
    # gate would quietly stop running on every landing.
    step = next(
        step
        for step in workflow["jobs"]["promote"]["steps"]
        if step.get("id") == "followup"
    )
    assert step["if"] == "steps.merge.outputs.merged == 'true'"
    assert "ci.yml promotion-gate.yml" in step["run"]
    assert "gh workflow run" in step["run"]
    # workflow_dispatch is the documented exception to the GITHUB_TOKEN rule,
    # so both targets must actually declare it.
    for name in ("ci.yml", "promotion-gate.yml"):
        other = yaml.safe_load(
            (REPO_ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
        )
        assert "workflow_dispatch" in (other.get("on") or other.get(True))


def test_the_job_has_a_timeout_and_does_not_run_in_a_fork(workflow: dict):
    job = workflow["jobs"]["promote"]
    assert isinstance(job["timeout-minutes"], int)
    assert "github.repository ==" in job["if"]


def test_the_merge_is_a_merge_commit_and_the_branch_is_never_deleted():
    text = executable_yaml()
    assert "gh pr merge" in text
    assert "--merge" in text
    assert "--squash" not in text and "--rebase" not in text
    # --delete-branch here would delete a long-lived branch.
    assert "--delete-branch" not in text
    # Never bypass the ruleset.
    assert "--admin" not in text


def test_the_promotion_pull_request_carries_the_same_label_as_the_manual_one():
    text = WORKFLOW.read_text(encoding="utf-8")
    manual = (REPO_ROOT / ".github" / "workflows" / "promote.yml").read_text(encoding="utf-8")
    assert "--label promotion" in text and "--label promotion" in manual


def test_every_decision_is_made_by_the_script_not_by_the_yaml(workflow: dict):
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "scripts/auto_promote.py decide" in text
    assert "scripts/auto_promote.py refused" in text
    # The summary is written on every run, including the ones that do nothing.
    summary_step = next(
        step
        for step in workflow["jobs"]["promote"]["steps"]
        if step.get("name") == "Publish the verdict to the job summary"
    )
    assert summary_step["if"] == "always()"
