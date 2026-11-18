"""Test-only command audit faults.

Production leaves this controller empty. Nothing here reads the environment.
A test assigns a fault in this process. The executor copies that assignment
into the worker payload. The worker applies only the known tokens.
"""

_COMMIT = frozenset({"command_started", "command_finished", "finished_once"})
_CRASH = frozenset({"before_finish"})


class AuditFaultController:
    def __init__(self) -> None:
        self.commit_failure: str | None = None
        self.crash_stage: str | None = None
        self.block_reconcile: bool = False

    def fail_commit(self, mode: str) -> None:
        if mode not in _COMMIT:
            raise ValueError(mode)
        self.commit_failure = mode

    def crash(self, stage: str) -> None:
        if stage not in _CRASH:
            raise ValueError(stage)
        self.crash_stage = stage

    def clear(self) -> None:
        self.commit_failure = None
        self.crash_stage = None
        self.block_reconcile = False

    def payload(self) -> dict[str, str | bool]:
        body: dict[str, str | bool] = {}
        if self.commit_failure is not None:
            body["audit_commit_failure"] = self.commit_failure
        if self.crash_stage is not None:
            body["audit_crash"] = self.crash_stage
        if self.block_reconcile:
            body["audit_block_reconcile"] = True
        return body

    def apply_payload(self, payload: dict) -> None:
        """Accept a fault the parent put in the worker payload. Ignore anything else."""
        self.clear()
        commit = payload.get("audit_commit_failure")
        crash = payload.get("audit_crash")
        if isinstance(commit, str) and commit in _COMMIT:
            self.commit_failure = commit
        if isinstance(crash, str) and crash in _CRASH:
            self.crash_stage = crash
        if payload.get("audit_block_reconcile") is True:
            self.block_reconcile = True


controller = AuditFaultController()
