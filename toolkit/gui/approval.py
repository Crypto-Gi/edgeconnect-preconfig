"""Upload approvals for the GUI (docs/gui/BUILD_PLAN.md step 2), the same way the automation GUI approves its writes.

The terminal is unchanged: the CLI uploads only with --approve. In the GUI the person types the Orchestrator's nickname; that
is exchanged for a single-use approval bound to one batch fingerprint (upload.run_batch records it) and one Orchestrator,
which the run's approver redeems just before the first upload. A batch that changed since it was checked has another
fingerprint, so it is refused and nothing is sent.
"""
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Sequence

APPROVAL_SECONDS = 600


class ApprovalError(Exception):
    """An approval was refused, is spent, has expired, or is for another batch or Orchestrator."""


@dataclass(frozen=True)
class Gate:
    prompt: str
    expected: str
    refusal: str


def upload_gates(nickname: str) -> List[Gate]:
    """A CSV upload is approved by typing the Orchestrator's nickname exactly (D9). upload --yaml needs none (decision 0017)."""
    return [Gate("Type the Orchestrator nickname {} to upload: ".format(nickname), nickname,
                 "upload approval refused: the nickname typed is not {}".format(nickname))]


@dataclass(frozen=True)
class _Grant:
    fingerprint: str
    orchestrator: str
    expires: float


class GuiApprovals:
    """Single-use approvals for the GUI: each is bound to one batch fingerprint and one Orchestrator, and expires."""

    def __init__(self, seconds: float = APPROVAL_SECONDS, clock: Callable[[], float] = time.monotonic) -> None:
        self._seconds = seconds
        self._clock = clock
        self._grants: Dict[str, _Grant] = {}
        self._lock = threading.Lock()

    def issue(self, gates: Sequence[Gate], answers: Sequence[str], fingerprint: str, orchestrator: str) -> str:
        """Check the typed answers against every gate and return an approval token for exactly this batch and Orchestrator."""
        if len(answers) != len(gates):
            raise ApprovalError("every confirmation must be answered")
        for gate, answer in zip(gates, answers):
            if answer != gate.expected:
                raise ApprovalError(gate.refusal)
        token = secrets.token_urlsafe(32)
        now = self._clock()
        with self._lock:
            self._grants = {key: grant for key, grant in self._grants.items() if grant.expires > now}
            self._grants[token] = _Grant(fingerprint, orchestrator, now + self._seconds)
        return token

    def redeem(self, token: str, fingerprint: str, orchestrator: str) -> None:
        """Use an approval once; raise ApprovalError if it is unknown, already used, expired, or for another batch or Orchestrator."""
        with self._lock:
            grant = self._grants.pop(token, None)
        if grant is None:
            raise ApprovalError("approval is unknown or was already used")
        if self._clock() >= grant.expires:
            raise ApprovalError("approval expired; check the batch and approve again")
        if grant.fingerprint != fingerprint:
            raise ApprovalError("the batch changed since it was approved (a row, the template, the CSV or the Orchestrator); check it and approve again")
        if grant.orchestrator != orchestrator:
            raise ApprovalError("approval is for a different Orchestrator")

    def approver(self, token: str, orchestrator: str) -> Callable[[str], None]:
        """The run context's approver for one upload run: redeems the token against the fingerprint run_batch passes it."""
        def approve(fingerprint: str) -> None:
            self.redeem(token, fingerprint, orchestrator)
        return approve
