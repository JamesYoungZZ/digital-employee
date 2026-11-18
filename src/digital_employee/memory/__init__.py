"""Persistent cross-task repository memory (`claude-mem` dual-mode engine & rule distillation)."""

from digital_employee.memory.claude_mem import (
    ClaudeMemService,
    MemoryObservation,
    ObservationType,
)

__all__ = [
    "ClaudeMemService",
    "MemoryObservation",
    "ObservationType",
]
