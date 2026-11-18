"""Evidence-Based Quality Gate: Fail-to-Pass + Anti-Tampering + Deterministic Static Checks."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from digital_employee.models.contract import TaskContract
from digital_employee.models.state import VerificationEvidence
from digital_employee.sandbox.worktree_runner import WorktreeSandbox
from digital_employee.security.dlp import check_path_policy

SKIP_OR_XFAIL_RE = re.compile(
    r"^\+.*(?:\bpytest\.skip\s*\(|\bpytest\.xfail\s*\(|@pytest\.mark\.skip\b|@pytest\.mark\.xfail\b|"
    r"@unittest\.skip\b|\b(?:it|test|describe)\.skip\s*\(|\bt\.Skip(?:Now|f)?\s*\()"
)
ASSERT_REMOVED_RE = re.compile(r"^-\s*(?:assert\b|expect\s*\()")
ASSERT_ADDED_RE = re.compile(r"^\+\s*(?:assert\b|with\s+pytest\.raises\b|expect\s*\()")
SWALLOW_EXCEPT_RE = re.compile(r"^\+\s*except\s+(?:Exception|BaseException)?\s*(?::|\s+as\s+\w+:)\s*pass\b")


def is_test_path(rel_path: str) -> bool:
    parts = rel_path.replace("\\", "/").split("/")
    name = parts[-1]
    return (
        "tests" in parts
        or "__tests__" in parts
        or name.startswith("test_")
        or name.endswith(("_test.py", "_test.go", ".test.ts", ".test.tsx", ".spec.ts", ".test.js"))
    )


def scan_test_diff_for_tampering(diff_text: str) -> list[str]:
    """Detect weakened tests in unified git diff of test files (Spec v2.0 Section 3.3.2)."""
    if not diff_text.strip():
        return []

    violations: list[str] = []
    removed_asserts = 0
    added_asserts = 0

    for line in diff_text.splitlines():
        if line.startswith(("---", "+++", "@@")):
            continue
        if SKIP_OR_XFAIL_RE.search(line):
            violations.append(f"Added test skip/xfail directive: `{line.strip()[:120]}`")
        if SWALLOW_EXCEPT_RE.search(line):
            violations.append(f"Swallowed exception in test: `{line.strip()[:120]}`")
        if ASSERT_REMOVED_RE.search(line):
            removed_asserts += 1
        elif ASSERT_ADDED_RE.search(line):
            added_asserts += 1

    if removed_asserts > added_asserts:
        violations.append(
            f"Net assertion reduction in test files: removed {removed_asserts} assert(s), added {added_asserts}."
        )
    return violations


@dataclass
class GateExecutionResult:
    evidence: VerificationEvidence
    diagnostic_log: str


NON_LOGIC_ASSET_SUFFIXES = (
    ".tsx",
    ".jsx",
    ".css",
    ".scss",
    ".less",
    ".html",
    ".md",
    ".mdx",
    ".yaml",
    ".yml",
    ".toml",
    ".svg",
)


def _is_pure_ui_or_config_change(changed_files: list[str]) -> bool:
    non_tests = [f for f in changed_files if not is_test_path(f)]
    if not non_tests:
        return False
    return all(f.lower().endswith(NON_LOGIC_ASSET_SUFFIXES) for f in non_tests)


class EvidenceGate:
    """Executes hard verification gates on a sandbox worktree commit."""

    def __init__(self, sandbox: WorktreeSandbox, contract: TaskContract) -> None:
        self.sandbox = sandbox
        self.contract = contract

    def evaluate(self, head_commit_sha: str, repro_test_target: str) -> GateExecutionResult:
        logs: list[str] = [
            f"=== Evidence Gate Evaluation for commit {head_commit_sha} ===",
            f"Base Commit: {self.contract.base_commit_sha}",
            f"Target Regression Test: {repro_test_target}",
        ]

        changed_files = self.sandbox.modified_files(self.contract.base_commit_sha)
        logs.append(f"Modified files ({len(changed_files)}): {changed_files}")

        use_regression_mode = (
            self.contract.verification_mode == "regression_and_build"
            or _is_pure_ui_or_config_change(changed_files)
        )
        mode_used = "regression_and_build" if use_regression_mode else "fail_to_pass"
        logs.append(f"Verification Mode: {mode_used}")

        # 1. Scope Compliance Check
        scope_violations: list[str] = []
        if not changed_files:
            scope_violations.append("No files were modified compared to base_commit_sha.")
        for rel in changed_files:
            ok, reason = check_path_policy(
                rel,
                permitted_scopes=self.contract.permitted_scopes,
                forbidden_paths=self.sandbox.profile.forbidden_paths,
            )
            if not ok:
                scope_violations.append(reason)
        scope_ok = len(scope_violations) == 0

        # 2. Anti-Tampering Check on Test Files
        test_files = [f for f in changed_files if is_test_path(f)]
        test_diff = (
            self.sandbox.git_diff(self.contract.base_commit_sha, paths=test_files)
            if test_files
            else ""
        )
        tampering_violations = scan_test_diff_for_tampering(test_diff)
        tests_not_weakened = len(tampering_violations) == 0
        if tampering_violations:
            logs.append(f"[FAIL] Anti-tampering violations: {tampering_violations}")
        else:
            logs.append("[PASS] Anti-tampering check clean.")

        # 3. Run Target Regression Test on HEAD (Must PASS)
        repro_passed_on_head = False
        repro_failed_on_base = False
        repro_targets = [
            t.strip() for t in re.split(r"[\s,]+", repro_test_target) if t.strip()
        ]

        if use_regression_mode:
            # UI / Refactor / Config / Docs mode: verify frontend build (if applicable) + existing regression tests!
            build_ok = True
            has_fe_edits = any(
                f.lower().endswith((".tsx", ".jsx", ".ts", ".css", ".html")) for f in changed_files
            )
            if has_fe_edits:
                for fe_dir in (self.sandbox.worktree_path / "frontend", self.sandbox.worktree_path):
                    if (fe_dir / "package.json").is_file():
                        fe_res = self.sandbox._exec(
                            ["npm", "run", "build"], cwd=fe_dir, timeout_seconds=120
                        )
                        build_ok = fe_res.exit_code == 0
                        logs.append(
                            f"\n--- [FRONTEND BUILD] {fe_dir.name} (exit={fe_res.exit_code}) ---\n"
                            f"{fe_res.combined_output[-2000:]}"
                        )
                        break

            fallback_targets = (
                repro_targets
                or list(self.sandbox.profile.default_suite_targets)
                or (["tests"] if (self.sandbox.worktree_path / "tests").is_dir() else [])
            )
            if fallback_targets:
                head_repro = self.sandbox.run_test_targets(fallback_targets, timeout_seconds=120)
                repro_passed_on_head = build_ok and head_repro.exit_code == 0
                repro_test_target = repro_test_target or ", ".join(fallback_targets)
                logs.append(
                    f"\n--- [HEAD REGRESSION] test {fallback_targets} (exit={head_repro.exit_code}) ---\n"
                    f"{head_repro.combined_output[-2500:]}"
                )
            else:
                repro_passed_on_head = build_ok
                repro_test_target = repro_test_target or "build_verification"
        elif not repro_targets:
            logs.append("[FAIL] Missing `repro_test_target` — cannot establish Fail-to-Pass proof.")
        else:
            head_repro = self.sandbox.run_test_targets(repro_targets)
            repro_passed_on_head = head_repro.exit_code == 0
            logs.append(
                f"\n--- [HEAD] test {repro_targets} (exit={head_repro.exit_code}) ---\n"
                f"{head_repro.combined_output[-2500:]}"
            )

            # 4. Run Target Regression Test on BASELINE with test overlay (Must FAIL)
            base_dir: Path | None = None
            try:
                base_dir = self.sandbox.create_detached_baseline_worktree(
                    self.contract.base_commit_sha
                )
                # Copy only modified/added test files onto baseline so new regression tests execute against unpatched code
                for tf in test_files:
                    src_file = self.sandbox.worktree_path / tf
                    dst_file = base_dir / tf
                    if src_file.is_file():
                        dst_file.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src_file, dst_file)

                base_repro = self.sandbox.run_test_targets(repro_targets, cwd=base_dir)
                # Pytest exit code 1 = tests collected and failed (valid Fail-to-Pass!).
                # Exit code 2/3/4 = interrupted / internal / CLI usage error (invalid!).
                is_pytest_runner = any("pytest" in p for p in self.sandbox.profile.test_cmd)
                if is_pytest_runner:
                    repro_failed_on_base = base_repro.exit_code == 1 or (
                        base_repro.exit_code != 0
                        and ("FAILED" in base_repro.combined_output or "ERRORS" in base_repro.combined_output)
                        and "usage: pytest" not in base_repro.combined_output
                    )
                else:
                    repro_failed_on_base = base_repro.exit_code != 0 and not base_repro.timed_out
                logs.append(
                    f"\n--- [BASE] test {repro_targets} (exit={base_repro.exit_code}) ---\n"
                    f"{base_repro.combined_output[-2500:]}"
                )
            finally:
                if base_dir is not None:
                    self.sandbox.cleanup_worktree(base_dir)

        # 5. Existing Related Test Suite on HEAD
        repro_files = [rt.split("::")[0] for rt in repro_targets if rt]
        suite_targets = list(
            dict.fromkeys(
                [t for t in [*repro_files, *self.sandbox.profile.default_suite_targets] if t]
            )
        )
        if suite_targets:
            suite_res = self.sandbox.run_test_targets(suite_targets, timeout_seconds=120)
            suite_passed = suite_res.exit_code == 0
            logs.append(
                f"\n--- [SUITE] pytest {suite_targets} (exit={suite_res.exit_code}) ---\n"
                f"{suite_res.combined_output[-2500:]}"
            )
        else:
            suite_passed = repro_passed_on_head

        # 6. Deterministic Static Analysis / Linter
        lint_res = self.sandbox.run_linter(changed_files)
        static_passed = lint_res.exit_code == 0
        logs.append(
            f"\n--- [LINT] (exit={lint_res.exit_code}) ---\n{lint_res.combined_output[-1500:]}"
        )

        evidence = VerificationEvidence(
            verified_commit_sha=head_commit_sha,
            repro_test_target=repro_test_target,
            verification_mode_used=mode_used,
            repro_test_failed_on_base=repro_failed_on_base,
            repro_test_passed_on_head=repro_passed_on_head,
            existing_test_suite_passed=suite_passed,
            tests_weakened_check=tests_not_weakened,
            scope_compliance_check=scope_ok,
            static_analysis_passed=static_passed,
            anti_tampering_violations=tampering_violations,
            scope_violations=scope_violations,
        )
        return GateExecutionResult(evidence=evidence, diagnostic_log="\n".join(logs))
