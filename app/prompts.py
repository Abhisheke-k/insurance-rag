"""Week 11: prompt versions that cannot drift silently.

Two of the Week 11 brief's mistakes are about prompts: fixing one without
recording the version bump, and blaming "the model changed" for a bad answer
without checking which prompt version produced it. Both come down to the same
requirement: every trace names the prompt version that built it, and that name
is only trustworthy if editing a prompt *forces* a new one.

This module is that mechanism, and it is deliberately small:

* every prompt the app sends to a model is registered below by name;
* ``prompt_lock.json`` pins, per name, a human-chosen version string and the
  SHA-256 of the prompt text that version denotes;
* ``tests/test_observability.py::test_prompt_lock_matches_the_shipped_prompts``
  recomputes the hashes, so changing a prompt's text without bumping its entry
  in the lock fails CI -- the version *is* the text, not a promise about it;
* :func:`version_for_system` maps a system prompt seen at the LLM seam back to
  its registered version, which is what ends up on every generation span.

Bumping a version::

    python -m app.prompts --bump claim-summary=claim-summary-v2

rewrites the lock entry with the new version and the current text's hash. The
command refuses to "bump" to the version already recorded, and refuses an unknown
name -- there is no flag that records a changed hash under an unchanged version.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

__all__ = [
    "LOCK_PATH",
    "check_lock",
    "load_lock",
    "prompt_version",
    "registered_prompts",
    "sha256_of",
    "version_for_system",
]

LOCK_PATH = Path(__file__).with_name("prompt_lock.json")
_ROOT = Path(__file__).resolve().parent.parent


def sha256_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def registered_prompts() -> dict[str, str]:
    """name -> the prompt text the app currently ships under that name.

    Imported lazily: the modules that own these prompts import the LLM layer, which in turn
    uses this registry to label spans, so a module-level import would be circular.
    """
    from app import agent, claims, generation, mcp_agent, team  # noqa: PLC0415

    prompts = {
        "ask-answer": generation.SYSTEM_PROMPT,
        "claim-summary": claims.SYSTEM_PROMPT,
        "claims-agent": agent.SYSTEM_PROMPT,
        "role:claims_agent": mcp_agent.SINGLE_AGENT_ROLE.brief,
        "role:coverage_analyst": team.ANALYST_ROLE.brief,
        "role:claims_clerk": team.CLERK_ROLE.brief,
        "role:manager": team.MANAGER_SYSTEM_PROMPT,
    }
    try:  # Week 12's triage role, present once app/triage.py exists
        from app import triage  # noqa: PLC0415

        prompts["role:claims_triage"] = triage.TRIAGE_ROLE.brief
    except ImportError:
        pass
    judge = _ROOT / "coursework" / "w6" / "judge_v2.txt"
    if judge.exists():
        prompts["judge"] = judge.read_text(encoding="utf-8")
    integrated = _ROOT / "coursework" / "w12" / "judge_integrated.txt"  # Week 12: the judge re-targeted at the triage card
    if integrated.exists():
        prompts["judge-integrated"] = integrated.read_text(encoding="utf-8")
    return prompts


def load_lock() -> dict[str, dict[str, str]]:
    if not LOCK_PATH.exists():
        return {}
    return json.loads(LOCK_PATH.read_text(encoding="utf-8"))


def prompt_version(name: str) -> str:
    """The version string recorded in the lock for ``name`` (``unlocked:<name>`` if absent)."""
    entry = load_lock().get(name)
    return entry["version"] if entry else f"unlocked:{name}"


def version_for_system(system: str) -> str:
    """Map a system prompt seen at the LLM seam to its registered version.

    Tool-using agents build their system prompt around a discovered tool catalogue, so the full
    text varies per run; those are recognised by their ``ROLE: <name>`` first line and versioned
    by the role brief. Anything not recognised is labelled with a hash prefix of its text, so
    "which prompt was this?" always has an answer, even if it is "one nobody registered".
    """
    lock = load_lock()
    first_line = system.splitlines()[0] if system else ""
    if first_line.startswith("ROLE: "):
        entry = lock.get(f"role:{first_line[len('ROLE: '):].strip()}")
        if entry:
            return entry["version"]
    digest = sha256_of(system)
    for name, text in registered_prompts().items():
        if sha256_of(text) == digest and name in lock:
            return lock[name]["version"]
    return f"unregistered:{digest[:8]}"


def check_lock() -> list[str]:
    """Problems found comparing the shipped prompts with the lock; empty means they agree."""
    lock = load_lock()
    problems: list[str] = []
    for name, text in registered_prompts().items():
        entry = lock.get(name)
        if entry is None:
            problems.append(f"{name}: not in prompt_lock.json (register it with --init)")
        elif entry["sha256"] != sha256_of(text):
            problems.append(
                f"{name}: text changed but the lock still records {entry['version']!r}; "
                f"bump it: python -m app.prompts --bump {name}=<new-version>"
            )
    for name in lock:
        if name not in registered_prompts():
            problems.append(f"{name}: in the lock but no longer registered")
    return problems


def _write(lock: dict[str, dict[str, str]]) -> None:
    LOCK_PATH.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--init", action="store_true", help="register prompts missing from the lock at <name>-v1")
    parser.add_argument("--bump", action="append", default=[], metavar="NAME=NEW_VERSION")
    parser.add_argument("--check", action="store_true", help="exit non-zero if any prompt drifted from the lock")
    arguments = parser.parse_args()

    lock = load_lock()
    prompts = registered_prompts()
    if arguments.init:
        for name, text in prompts.items():
            lock.setdefault(name, {"version": f"{name.replace(':', '-')}-v1", "sha256": sha256_of(text)})
        _write(lock)
    for item in arguments.bump:
        name, _, version = item.partition("=")
        if name not in prompts:
            parser.error(f"unknown prompt {name!r}; registered: {sorted(prompts)}")
        if lock.get(name, {}).get("version") == version:
            parser.error(f"{name} is already at {version!r}: a bump must change the version")
        lock[name] = {"version": version, "sha256": sha256_of(prompts[name])}
        _write(lock)
    problems = check_lock()
    for problem in problems:
        print(problem)
    if arguments.check and problems:
        raise SystemExit(1)
    for name, entry in sorted(load_lock().items()):
        print(f"{name:26} {entry['version']:34} {entry['sha256'][:12]}")


if __name__ == "__main__":
    main()
