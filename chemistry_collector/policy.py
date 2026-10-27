"""Runtime journal policy derived from V6.3 feasibility measurements."""
from dataclasses import dataclass
import json
from pathlib import Path

from .models import Journal


@dataclass(frozen=True)
class JournalPolicy:
    enabled: bool = True
    priority: int = 50
    max_download_attempts: int = 80
    reason: str = ""
    measured_attempts: int = 0
    measured_successes: int = 0

    @property
    def measured_success_rate(self) -> float:
        if not self.measured_attempts:
            return 0.0
        return self.measured_successes / self.measured_attempts


DEFAULT_POLICY = JournalPolicy()


def load_journal_policies(path: Path | None) -> dict[str, JournalPolicy]:
    if path is None:
        return {}
    if not path.is_file():
        raise FileNotFoundError(f"Journal runtime policy missing: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Journal runtime policy must be a JSON object")
    defaults = data.get("defaults") or {}
    rows = data.get("journals") or {}
    if not isinstance(defaults, dict) or not isinstance(rows, dict):
        raise ValueError("Journal runtime policy requires object defaults/journals")
    default = _policy_from_row(defaults, DEFAULT_POLICY)
    result: dict[str, JournalPolicy] = {}
    for name, row in rows.items():
        if not isinstance(row, dict):
            raise ValueError(f"Invalid journal policy: {name}")
        result[str(name)] = _policy_from_row(row, default)
    result["__default__"] = default
    return result


def _policy_from_row(row: dict, base: JournalPolicy) -> JournalPolicy:
    policy = JournalPolicy(
        enabled=bool(row.get("enabled", base.enabled)),
        priority=int(row.get("priority", base.priority)),
        max_download_attempts=int(
            row.get("max_download_attempts", base.max_download_attempts)
        ),
        reason=str(row.get("reason", base.reason)).strip(),
        measured_attempts=int(row.get("measured_attempts", base.measured_attempts)),
        measured_successes=int(row.get("measured_successes", base.measured_successes)),
    )
    if policy.priority < 0 or policy.max_download_attempts <= 0:
        raise ValueError("Journal priority must be >=0 and attempt budget must be >0")
    if min(policy.measured_attempts, policy.measured_successes) < 0:
        raise ValueError("Measured attempts/successes cannot be negative")
    if policy.measured_successes > policy.measured_attempts:
        raise ValueError("Measured successes cannot exceed attempts")
    return policy


def policy_for(policies: dict[str, JournalPolicy], journal: Journal) -> JournalPolicy:
    return policies.get(journal.name, policies.get("__default__", DEFAULT_POLICY))


def apply_journal_policies(
    journals: list[Journal],
    policies: dict[str, JournalPolicy],
    include_low_yield: bool = False,
) -> tuple[list[Journal], list[tuple[Journal, JournalPolicy]]]:
    enabled: list[tuple[int, int, Journal]] = []
    skipped: list[tuple[Journal, JournalPolicy]] = []
    for index, journal in enumerate(journals):
        policy = policy_for(policies, journal)
        if policy.enabled or include_low_yield:
            enabled.append((policy.priority, index, journal))
        else:
            skipped.append((journal, policy))
    enabled.sort(key=lambda row: (row[0], row[1]))
    return [row[2] for row in enabled], skipped

