"""Verification package."""

from digital_employee.verification.evidence_gate import (
    EvidenceGate,
    GateExecutionResult,
    is_test_path,
    scan_test_diff_for_tampering,
)

__all__ = [
    "EvidenceGate",
    "GateExecutionResult",
    "is_test_path",
    "scan_test_diff_for_tampering",
]
