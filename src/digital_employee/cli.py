"""Rich CLI entrypoint (`de`) for AI Digital Employee v2.0."""

from __future__ import annotations

from pathlib import Path

import typer
import uvicorn
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from digital_employee.context.decision_ledger import DecisionLedger
from digital_employee.models.state import DurableTaskState
from digital_employee.onboarding import onboard_repository
from digital_employee.orchestrator import DelegationOrchestrator
from digital_employee.settings import load_settings

app = typer.Typer(
    name="de",
    help="AI Digital Employee v2.0 — Dependable Engineering Delegation CLI",
    no_args_is_help=True,
)
console = Console()


def _print_task_summary(state: DurableTaskState, *, show_diff: bool = False) -> None:
    c = state.contract
    lines = [
        f"[bold]Task ID[/bold]: {state.task_id}   [bold]Status[/bold]: [cyan]{state.lifecycle_status}[/cyan]",
        f"[bold]Objective[/bold]: {c.objective}",
        f"[bold]Repo & Base[/bold]: {c.target_repo} ({c.base_branch} @ {c.base_commit_sha[:10]}) -> [green]{state.branch_name}[/green]",
        f"[bold]Worktree[/bold]: {state.worktree_path}",
        f"[bold]Permitted Scopes[/bold]: {', '.join(c.permitted_scopes)}",
        f"[bold]Out of Scope[/bold]: {', '.join(c.out_of_scope)}",
        f"[bold]Cost / Elapsed[/bold]: ${state.total_cost_usd:.4f} / {state.agent_elapsed_seconds:.1f}s (Est. Net Saved: {state.estimated_net_minutes_saved} min)",
    ]
    if state.deliverable_pr_url:
        lines.append(f"[bold]Draft PR[/bold]: {state.deliverable_pr_url}")

    if c.unresolved_decisions:
        lines.append("\n[bold yellow]⏸️ Unresolved Decisions (5s Clarification Needed):[/bold yellow]")
        for i, dec in enumerate(c.unresolved_decisions):
            lines.append(
                f"  [{i}] ({dec.impact_scope}) {dec.question}\n"
                f"      Options: {dec.options} | Default: {dec.default_assumption}"
            )

    if state.verification_evidence:
        ev = state.verification_evidence
        lines.append("\n[bold green]🧪 Evidence-Based Quality Gate:[/bold green]")
        lines.append(f"  • Target Regression Test: `{ev.repro_test_target}`")
        lines.append(f"  • Failed on Baseline ({c.base_commit_sha[:8]}): {ev.repro_test_failed_on_base}")
        lines.append(f"  • Passed on Head ({ev.verified_commit_sha[:8]}): {ev.repro_test_passed_on_head}")
        lines.append(f"  • Existing Suite Passed: {ev.existing_test_suite_passed}")
        lines.append(f"  • Anti-Tampering Check Clean: {ev.tests_weakened_check}")
        lines.append(f"  • Scope & Static Analysis Clean: {ev.scope_compliance_check and ev.static_analysis_passed}")
        if ev.reviewer_report:
            lines.append(f"  • Reviewer Summary: {ev.reviewer_report.summary}")
            lines.append("  • [yellow]Unverified Risks Disclosed[/yellow]:")
            for r in ev.reviewer_report.unverified_risks:
                lines.append(f"      - {r}")

    if state.handoff_bundle:
        hb = state.handoff_bundle
        lines.append(f"\n[bold red]🤝 Handoff Bundle ({hb.failure_class}):[/bold red] {hb.reason}")
        lines.append(f"  • Next Step: {hb.recommended_next_step}")

    console.print(Panel("\n".join(lines), title=f"Task {state.task_id}", expand=False))

    if show_diff and state.patch_artifact_uri and state.patch_artifact_uri.startswith("file://"):
        p = Path(state.patch_artifact_uri.removeprefix("file://"))
        if p.is_file():
            console.print(Syntax(p.read_text(encoding="utf-8"), "diff", line_numbers=False))


@app.command("repos")
def cmd_repos() -> None:
    """List registered target repositories and active DecisionLedger policies."""
    settings = load_settings()
    profiles = settings.load_repositories()
    table = Table(title="Registered Target Repositories")
    table.add_column("Repo ID", style="cyan", no_wrap=True)
    table.add_column("GitHub Repo")
    table.add_column("Base Branch")
    table.add_column("Active Policies")
    table.add_column("Local Path")

    for p in profiles.values():
        ledger = DecisionLedger.from_repo(Path(p.local_path), p.decision_ledger_path)
        table.add_row(
            p.repo_id,
            p.github_repo,
            p.default_base_branch,
            str(len(ledger.active_policies())),
            p.local_path,
        )
    console.print(table)


@app.command("onboard")
def cmd_onboard(
    source: str = typer.Argument(
        ..., help="Local path, Git SSH/HTTPS URL, or GitHub `owner/repo` shorthand."
    ),
    repo_id: str | None = typer.Option(None, "--id", help="Custom repository ID."),
    base_branch: str | None = typer.Option(None, "--branch", "-b", help="Override default base branch."),
    bootstrap_venv: bool = typer.Option(
        False, "--bootstrap-venv", help="Create `.venv` via `uv` if missing for Python repos."
    ),
) -> None:
    """Auto-onboard a new target Git repository (clone, detect test runner, register)."""
    settings = load_settings()
    profile = onboard_repository(
        settings,
        source,
        repo_id=repo_id,
        base_branch=base_branch,
        bootstrap_venv=bootstrap_venv,
    )
    console.print(
        Panel(
            f"[bold green]Onboarded `{profile.repo_id}` successfully![/bold green]\n"
            f"• GitHub Slug: {profile.github_repo}\n"
            f"• Local Path: {profile.local_path}\n"
            f"• Default Base Branch: {profile.default_base_branch}\n"
            f"• Test Runner: {' '.join(profile.pytest_cmd)}",
            title="Repository Onboarded",
            expand=False,
        )
    )


@app.command("delegate")
def cmd_delegate(
    request: str = typer.Argument(..., help="Natural language bug fix or engineering delegation request."),
    repo: str = typer.Option("modular-rag", "--repo", "-r", help="Target repository ID."),
    base_branch: str | None = typer.Option(None, "--base", "-b", help="Override base branch."),
    accept_defaults: bool = typer.Option(
        False, "--accept-defaults", help="Auto-confirm any minor unresolved decisions using default assumptions."
    ),
    draft_only: bool = typer.Option(
        False, "--draft-only", help="Draft the TaskContract and stop before sandbox coding."
    ),
    publish_pr: bool = typer.Option(
        False, "--publish-pr", help="Push `ai-agent/*` branch and open GitHub Draft PR if DE_GITHUB_TOKEN is set."
    ),
    show_diff: bool = typer.Option(True, "--diff/--no-diff", help="Display resulting patch diff."),
) -> None:
    """Draft a TaskContract, execute sandbox repair + Fail-to-Pass verification, and deliver evidence."""
    orch = DelegationOrchestrator()
    state = orch.submit_task(
        raw_request=request,
        target_repo=repo,
        base_branch=base_branch,
        accept_defaults=accept_defaults,
        auto_execute=not draft_only,
        publish_pr=publish_pr,
        source_channel="cli",
    )
    _print_task_summary(state, show_diff=show_diff)


@app.command("list")
def cmd_list(
    status: str | None = typer.Option(None, "--status", "-s", help="Filter by lifecycle status."),
    limit: int = typer.Option(20, "--limit", "-n"),
) -> None:
    """Display the durable task queue."""
    orch = DelegationOrchestrator()
    tasks = orch.store.list_tasks(status=status, limit=limit)
    table = Table(title="Digital Employee Work Queue")
    table.add_column("Task ID", style="cyan", no_wrap=True)
    table.add_column("Repo")
    table.add_column("Status", style="bold")
    table.add_column("Fail->Pass")
    table.add_column("Cost")
    table.add_column("Objective")

    for t in tasks:
        ev = t.verification_evidence
        f2p = (
            "PASS"
            if (ev and ev.repro_test_failed_on_base and ev.repro_test_passed_on_head)
            else "-"
        )
        table.add_row(
            t.task_id,
            t.contract.target_repo,
            t.lifecycle_status,
            f2p,
            f"${t.total_cost_usd:.4f}",
            t.contract.objective[:68],
        )
    console.print(table)


@app.command("show")
def cmd_show(
    task_id: str = typer.Argument(...),
    diff: bool = typer.Option(True, "--diff/--no-diff"),
) -> None:
    """Inspect a task's contract, verification evidence, checkpoints, and diff."""
    orch = DelegationOrchestrator()
    state = orch.store.get_task(task_id)
    if state is None:
        console.print(f"[red]Task '{task_id}' not found.[/red]")
        raise typer.Exit(code=1)
    _print_task_summary(state, show_diff=diff)


@app.command("clarify")
def cmd_clarify(
    task_id: str = typer.Argument(...),
    answer: list[str] = typer.Option(
        default=[], help="Answer mapping `0=Option A` or `Question=Answer`."
    ),
    use_defaults: bool = typer.Option(False, "--use-defaults"),
    publish_pr: bool = typer.Option(False, "--publish-pr"),
) -> None:
    """Resolve pending clarification questions and resume execution."""
    parsed: dict[str, str] = {}
    for item in answer:
        if "=" in item:
            k, v = item.split("=", 1)
            parsed[k.strip()] = v.strip()
    orch = DelegationOrchestrator()
    state = orch.resolve_clarification(
        task_id,
        answers=parsed,
        use_defaults=use_defaults or not parsed,
        auto_execute=True,
        publish_pr=publish_pr,
    )
    _print_task_summary(state, show_diff=True)


@app.command("followup")
def cmd_followup(
    task_id: str = typer.Argument(...),
    comment: str = typer.Argument(..., help="Review comment or follow-up adjustment."),
    publish_pr: bool = typer.Option(False, "--publish-pr"),
) -> None:
    """Apply a post-delivery / PR review follow-up change in the task's isolated worktree."""
    orch = DelegationOrchestrator()
    state = orch.handle_pr_review_comment(
        task_id,
        comment_body=comment,
        publish_pr=publish_pr,
    )
    _print_task_summary(state, show_diff=True)


@app.command("control")
def cmd_control(
    task_id: str = typer.Argument(...),
    action: str = typer.Argument(..., help="accept | abandon | cancel | publish_pr"),
    note: str = typer.Option("", "--note"),
) -> None:
    """Mark task accepted/abandoned, publish Draft PR, or cancel into a Handoff Bundle."""
    orch = DelegationOrchestrator()
    state = orch.update_scope_or_cancel(task_id, action=action, note=note)
    _print_task_summary(state, show_diff=False)


@app.command("metrics")
def cmd_metrics() -> None:
    """Show Spec v2.0 funnel conversion & Net Human Time Saved metrics."""
    orch = DelegationOrchestrator()
    m = orch.store.compute_funnel_metrics()
    table = Table(title="V1 Delegation Funnel & Economics Metrics")
    table.add_column("Metric", style="bold")
    table.add_column("Value", style="green")
    for k, v in m.items():
        table.add_row(k, str(v))
    console.print(table)


@app.command("serve")
def cmd_serve(
    host: str | None = typer.Option(None, "--host"),
    port: int | None = typer.Option(None, "--port"),
) -> None:
    """Start the lightweight FastAPI Web Control Plane & Feishu/GitHub webhook server."""
    settings = load_settings()
    bind_host = host or settings.web_host
    bind_port = port or settings.web_port
    console.print(
        f"[bold green]Starting AI Digital Employee Control Plane at http://{bind_host}:{bind_port}[/bold green]"
    )
    uvicorn.run(
        "digital_employee.web.app:create_app",
        factory=True,
        host=bind_host,
        port=bind_port,
        reload=False,
    )


if __name__ == "__main__":
    app()
