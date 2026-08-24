#!/usr/bin/env python3
"""Repository-backed protocol tooling for multi-agent collaboration.

The module deliberately uses only Python's standard library. Workflow files use
JSON syntax (which is also valid YAML) so validation works on a fresh checkout
without installing dependencies.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

PROTOCOL_VERSION = "1.0"
ROLES = ("orchestrator", "analyzer", "worker", "reviewer", "tester")
TERMINAL_STATES = {"COMPLETED", "BLOCKED", "HUMAN_REVIEW_REQUIRED"}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
ASSIGNMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")


class ProtocolError(RuntimeError):
    """A user-facing protocol validation error."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def timestamp_slug() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def repository_root(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    return Path(__file__).resolve().parents[2]


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProtocolError(f"Missing required file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProtocolError(f"Expected a JSON object in {path}")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def git_head(root: Path) -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None


def load_state(root: Path) -> dict[str, Any]:
    return read_json(root / ".ai" / "STATE.json")


def load_config(root: Path) -> dict[str, Any]:
    return read_json(root / ".ai" / "CONFIG.yml")


def workflow_path(root: Path, workflow: str) -> Path:
    if not ID_RE.fullmatch(workflow):
        raise ProtocolError(f"Invalid workflow id: {workflow!r}")
    return root / ".ai" / "WORKFLOWS" / f"{workflow}.yml"


def load_workflow(root: Path, workflow: str) -> dict[str, Any]:
    value = read_json(workflow_path(root, workflow))
    if value.get("id") != workflow:
        raise ProtocolError(f"Workflow id in {workflow}.yml does not match its filename")
    return value


def ensure_relative_file(root: Path, relative: str, label: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ProtocolError(f"{label} points outside the repository: {relative}") from exc
    if not candidate.is_file():
        raise ProtocolError(f"{label} does not exist: {relative}")
    return candidate


def assignment_errors(assignment: Any, *, pending: bool) -> list[str]:
    errors: list[str] = []
    if not isinstance(assignment, dict):
        return ["assignment must be an object"]
    assignment_id = assignment.get("id")
    if not isinstance(assignment_id, str) or not ASSIGNMENT_RE.fullmatch(assignment_id):
        errors.append("assignment.id must contain 3-128 safe characters")
    if assignment.get("role") not in ROLES:
        errors.append(f"assignment.role must be one of: {', '.join(ROLES)}")
    for field in ("scope", "instructions"):
        if not isinstance(assignment.get(field), str) or not assignment[field].strip():
            errors.append(f"assignment.{field} must be a non-empty string")
    criteria = assignment.get("acceptance_criteria")
    if not isinstance(criteria, list) or not criteria or not all(isinstance(item, str) and item.strip() for item in criteria):
        errors.append("assignment.acceptance_criteria must be a non-empty string array")
    if pending and assignment.get("status") != "pending":
        errors.append("active assignment.status must be 'pending'")
    return errors


def validate_workflow(value: dict[str, Any], filename: str) -> list[str]:
    errors: list[str] = []
    prefix = f"{filename}: "
    if value.get("protocol_version") != PROTOCOL_VERSION:
        errors.append(prefix + f"protocol_version must be {PROTOCOL_VERSION}")
    workflow_id = value.get("id")
    if not isinstance(workflow_id, str) or not ID_RE.fullmatch(workflow_id):
        errors.append(prefix + "id is invalid")
    if not isinstance(value.get("description"), str) or not value["description"].strip():
        errors.append(prefix + "description is required")
    if not isinstance(value.get("default_max_iterations"), int) or not 1 <= value["default_max_iterations"] <= 20:
        errors.append(prefix + "default_max_iterations must be between 1 and 20")
    phases = value.get("phases")
    transitions = value.get("transitions")
    if not isinstance(phases, dict) or not phases:
        errors.append(prefix + "phases must be a non-empty object")
        return errors
    if not isinstance(transitions, dict):
        errors.append(prefix + "transitions must be an object")
        return errors
    known_sources = {"START", *phases.keys()}
    known_targets = set(phases.keys())
    for phase, definition in phases.items():
        if not isinstance(definition, dict):
            errors.append(prefix + f"phase {phase} must be an object")
            continue
        allowed_roles = definition.get("allowed_roles")
        if not isinstance(allowed_roles, list) or any(role not in ROLES for role in allowed_roles):
            errors.append(prefix + f"phase {phase} has invalid allowed_roles")
        terminal = definition.get("terminal")
        if not isinstance(terminal, bool):
            errors.append(prefix + f"phase {phase}.terminal must be boolean")
        if terminal and allowed_roles:
            errors.append(prefix + f"terminal phase {phase} cannot assign roles")
    for source, targets in transitions.items():
        if source not in known_sources:
            errors.append(prefix + f"transition source {source} is unknown")
        if not isinstance(targets, list) or any(target not in known_targets for target in targets):
            errors.append(prefix + f"transition targets for {source} are invalid")
    if "START" not in transitions:
        errors.append(prefix + "transitions.START is required")
    return errors


def validate_state(root: Path, state: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    required = {
        "protocol_version",
        "revision",
        "task_id",
        "task_file",
        "workflow",
        "state",
        "previous_state",
        "completed_roles",
        "completed_steps",
        "active_assignments",
        "next_role",
        "iteration",
        "max_iterations",
        "last_role",
        "last_relevant_commit",
        "reason",
        "summary",
        "blocked",
        "updated_at",
    }
    missing = sorted(required - state.keys())
    if missing:
        errors.append("STATE.json missing fields: " + ", ".join(missing))
        return errors
    if state["protocol_version"] != PROTOCOL_VERSION:
        errors.append(f"STATE.json protocol_version must be {PROTOCOL_VERSION}")
    if not isinstance(state["revision"], int) or state["revision"] < 0:
        errors.append("STATE.json revision must be a non-negative integer")
    if not isinstance(state["completed_roles"], list) or any(role not in ROLES for role in state["completed_roles"]):
        errors.append("STATE.json completed_roles contains an unknown role")
    if not isinstance(state["completed_steps"], list) or not all(isinstance(item, str) for item in state["completed_steps"]):
        errors.append("STATE.json completed_steps must be a string array")
    if not isinstance(state["active_assignments"], list):
        errors.append("STATE.json active_assignments must be an array")
        assignments: list[dict[str, Any]] = []
    else:
        assignments = state["active_assignments"]
        identifiers: set[str] = set()
        for index, assignment in enumerate(assignments):
            for error in assignment_errors(assignment, pending=True):
                errors.append(f"STATE.json active_assignments[{index}]: {error}")
            if isinstance(assignment, dict) and isinstance(assignment.get("id"), str):
                if assignment["id"] in identifiers:
                    errors.append(f"STATE.json has duplicate assignment id {assignment['id']}")
                identifiers.add(assignment["id"])
    if not isinstance(state["iteration"], int) or not isinstance(state["max_iterations"], int):
        errors.append("STATE.json iteration values must be integers")
    elif state["iteration"] < 0 or state["max_iterations"] < 1 or state["iteration"] > state["max_iterations"]:
        errors.append("STATE.json iteration must be between 0 and max_iterations")
    for field in ("reason", "summary"):
        if not isinstance(state[field], str) or not state[field].strip():
            errors.append(f"STATE.json {field} must be non-empty")

    task_id = state["task_id"]
    if task_id is None:
        if state["state"] != "IDLE":
            errors.append("STATE.json without a task must be IDLE")
        if state["task_file"] is not None or state["workflow"] is not None:
            errors.append("STATE.json IDLE task_file and workflow must be null")
        if assignments or state["next_role"] is not None:
            errors.append("STATE.json IDLE cannot have active assignments")
        return errors

    if not isinstance(task_id, str) or not ID_RE.fullmatch(task_id):
        errors.append("STATE.json task_id is invalid")
    if not isinstance(state["task_file"], str):
        errors.append("STATE.json task_file must be a path")
    else:
        try:
            ensure_relative_file(root, state["task_file"], "task_file")
        except ProtocolError as exc:
            errors.append(str(exc))
    if not isinstance(state["workflow"], str):
        errors.append("STATE.json workflow must be a string")
        return errors
    try:
        workflow = load_workflow(root, state["workflow"])
    except ProtocolError as exc:
        errors.append(str(exc))
        return errors
    phases = workflow["phases"]
    valid_states = {"ORCHESTRATION_REQUIRED", *phases.keys()}
    if state["state"] not in valid_states:
        errors.append(f"STATE.json state {state['state']!r} is not valid for workflow {state['workflow']}")
    if state["previous_state"] is not None and state["previous_state"] not in phases:
        errors.append("STATE.json previous_state is invalid")

    if state["state"] == "ORCHESTRATION_REQUIRED":
        if len(assignments) != 1 or (assignments and assignments[0].get("role") != "orchestrator"):
            errors.append("ORCHESTRATION_REQUIRED must have exactly one orchestrator assignment")
    elif state["state"] in TERMINAL_STATES:
        if assignments:
            errors.append(f"terminal state {state['state']} cannot have assignments")
    elif state["state"] in phases:
        definition = phases[state["state"]]
        allowed = definition.get("allowed_roles", [])
        if definition.get("terminal"):
            if assignments:
                errors.append(f"terminal phase {state['state']} cannot have assignments")
        elif not assignments:
            errors.append(f"active phase {state['state']} requires at least one assignment")
        for assignment in assignments:
            if isinstance(assignment, dict) and assignment.get("role") not in allowed:
                errors.append(f"role {assignment.get('role')} is not allowed in phase {state['state']}")

    expected_role = assignments[0]["role"] if assignments and isinstance(assignments[0], dict) and "role" in assignments[0] else None
    if state["next_role"] != expected_role:
        errors.append("STATE.json next_role must equal the first active assignment role, or null")
    config = load_config(root)
    max_parallel = config.get("dispatch", {}).get("max_parallel_assignments", 1)
    if isinstance(max_parallel, int) and len(assignments) > max_parallel:
        errors.append(f"STATE.json exceeds configured max_parallel_assignments ({max_parallel})")
    return errors


def validate_repository(root: Path) -> list[str]:
    required_files = [
        ".ai/AGENTS.md",
        ".ai/STATE.json",
        ".ai/CONTEXT.md",
        ".ai/DECISION.json",
        ".ai/CONFIG.yml",
        ".ai/SCHEMAS/state.schema.json",
        ".ai/SCHEMAS/decision.schema.json",
        ".ai/SCHEMAS/result.schema.json",
        ".ai/SCHEMAS/dispatch.schema.json",
    ]
    required_files.extend(f".ai/ROLES/{role}.md" for role in ROLES)
    errors: list[str] = []
    for relative in required_files:
        path = root / relative
        if not path.is_file():
            errors.append(f"Missing required file: {relative}")
        elif path.stat().st_size == 0:
            errors.append(f"Required file is empty: {relative}")
    if errors:
        return errors
    try:
        state = load_state(root)
        errors.extend(validate_state(root, state))
    except ProtocolError as exc:
        errors.append(str(exc))
    try:
        config = load_config(root)
        if config.get("protocol_version") != PROTOCOL_VERSION:
            errors.append(f"CONFIG.yml protocol_version must be {PROTOCOL_VERSION}")
        bindings = config.get("role_bindings")
        if not isinstance(bindings, dict) or set(bindings) != set(ROLES):
            errors.append("CONFIG.yml role_bindings must define exactly the five protocol roles")
        max_parallel = config.get("dispatch", {}).get("max_parallel_assignments")
        if not isinstance(max_parallel, int) or not 1 <= max_parallel <= 10:
            errors.append("CONFIG.yml max_parallel_assignments must be between 1 and 10")
    except ProtocolError as exc:
        errors.append(str(exc))
    for path in sorted((root / ".ai" / "WORKFLOWS").glob("*.yml")):
        try:
            errors.extend(validate_workflow(read_json(path), path.name))
        except ProtocolError as exc:
            errors.append(str(exc))
    if not list((root / ".ai" / "WORKFLOWS").glob("*.yml")):
        errors.append("At least one workflow definition is required")
    for relative in (
        ".ai/DECISION.json",
        ".ai/SCHEMAS/state.schema.json",
        ".ai/SCHEMAS/decision.schema.json",
        ".ai/SCHEMAS/result.schema.json",
        ".ai/SCHEMAS/dispatch.schema.json",
    ):
        try:
            read_json(root / relative)
        except ProtocolError as exc:
            errors.append(str(exc))
    if not (root / ".ai" / "CONTEXT.md").read_text(encoding="utf-8").strip():
        errors.append("CONTEXT.md must not be empty")
    return errors


def make_assignment(
    assignment_id: str,
    role: str,
    scope: str,
    instructions: str,
    criteria: Iterable[str],
) -> dict[str, Any]:
    value = {
        "id": assignment_id,
        "role": role,
        "scope": scope,
        "instructions": instructions,
        "acceptance_criteria": list(criteria),
        "status": "pending",
    }
    errors = assignment_errors(value, pending=True)
    if errors:
        raise ProtocolError("Invalid assignment: " + "; ".join(errors))
    return value


def orchestrator_assignment(task_id: str, revision: int, reason: str) -> dict[str, Any]:
    return make_assignment(
        f"{task_id}:orchestrate:r{revision}",
        "orchestrator",
        "Repository state, active task, current context, and latest role results",
        reason,
        ["Record a valid decision", "Select only useful next work", "Stop or escalate when appropriate"],
    )


def initial_decision(task_id: str | None = None, workflow: str | None = None, revision: int = 0) -> dict[str, Any]:
    return {
        "$schema": "./SCHEMAS/decision.schema.json",
        "protocol_version": PROTOCOL_VERSION,
        "decision_id": None,
        "task_id": task_id,
        "based_on_revision": revision,
        "workflow": workflow,
        "state": "IDLE" if task_id is None else "ORCHESTRATION_REQUIRED",
        "assignments": [],
        "iteration": 0,
        "reason": "No orchestrator decision has been recorded yet.",
        "summary": "Waiting for an active task." if task_id is None else "Waiting for orchestrator evaluation.",
        "expected_outcome": None,
        "mark_steps_completed": [],
        "created_at": None,
    }


CONTEXT_START = "<!-- ai-generated-state:start -->"
CONTEXT_END = "<!-- ai-generated-state:end -->"


def refresh_context(root: Path, state: dict[str, Any], *, reset_notes: bool = False) -> None:
    """Refresh current facts while preserving the human/agent-maintained notes section."""
    context_path = root / ".ai" / "CONTEXT.md"
    existing = context_path.read_text(encoding="utf-8") if context_path.exists() else ""
    if not reset_notes and CONTEXT_END in existing:
        durable_notes = existing.split(CONTEXT_END, 1)[1].lstrip("\n")
    else:
        durable_notes = "## Durable decisions and risks\n\n- None recorded beyond the canonical state.\n"

    completed = state.get("completed_steps") or []
    completed_lines = "\n".join(f"- {step}" for step in completed) or "- No task step has been completed yet."
    completed_roles = state.get("completed_roles") or []
    if completed_roles:
        completed_lines += "\n- Roles reported so far: " + ", ".join(completed_roles) + "."
    assignments = state.get("active_assignments") or []
    if assignments:
        remaining_lines = "\n".join(
            f"- `{item['role']}` / `{item['id']}` — {item['scope']}" for item in assignments
        )
    elif state.get("state") in TERMINAL_STATES:
        remaining_lines = f"- No assignment remains; the task is `{state['state']}`."
    else:
        remaining_lines = "- No assignment is currently recorded."
    blocked = state.get("blocked")
    if blocked:
        risk_lines = (
            f"- {blocked.get('reason', 'A blocker is recorded.')}\n"
            f"- Requested action: {blocked.get('requested_action') or 'not specified'}"
        )
    else:
        risk_lines = "- No blocker is recorded in canonical state; see durable notes and the task for non-blocking risks."
    if state.get("task_id") is None:
        task_lines = "No active task."
    else:
        task_lines = (
            f"**`{state['task_id']}`** using `{state['workflow']}`; state `{state['state']}`, "
            f"iteration {state['iteration']}/{state['max_iterations']}.\n\n"
            f"Task file: `{state['task_file']}`\n\n{state['summary']}"
        )
    generated = (
        f"# Current AI Context\n\n{CONTEXT_START}\n"
        f"Last synchronized: {state.get('updated_at') or 'not yet'} (state revision {state['revision']})\n\n"
        f"## Current task\n\n{task_lines}\n\n"
        f"## Completed\n\n{completed_lines}\n\n"
        f"## Remaining\n\n{remaining_lines}\n\n"
        f"## Current decision\n\n- {state['reason']}\n"
        f"- Last role: `{state.get('last_role') or 'none'}`; relevant commit: "
        f"`{state.get('last_relevant_commit') or 'not recorded'}`.\n\n"
        f"## Known blockers\n\n{risk_lines}\n{CONTEXT_END}\n\n"
    )
    context_path.write_text(generated + durable_notes.rstrip() + "\n", encoding="utf-8")


def start_task(args: argparse.Namespace, root: Path) -> None:
    task_id = args.id
    if not ID_RE.fullmatch(task_id):
        raise ProtocolError("Task id must use 3-64 lowercase letters, numbers, dots, underscores, or hyphens")
    if not args.title.strip() or not args.description.strip():
        raise ProtocolError("Task title and description must be non-empty")
    state = load_state(root)
    if state.get("task_id") is not None and state.get("state") not in TERMINAL_STATES:
        raise ProtocolError(f"Task {state['task_id']} is still active ({state['state']}); complete or escalate it first")
    workflow = load_workflow(root, args.workflow)
    max_iterations = args.max_iterations or workflow["default_max_iterations"]
    if not 1 <= max_iterations <= 20:
        raise ProtocolError("max_iterations must be between 1 and 20")
    task_path = root / ".ai" / "TASKS" / f"{task_id}.md"
    if task_path.exists() and not args.reuse:
        raise ProtocolError(f"Task file already exists: {task_path.relative_to(root)} (use --reuse to reactivate it)")
    now = utc_now()
    if not task_path.exists() or not args.reuse:
        task_path.parent.mkdir(parents=True, exist_ok=True)
        task_path.write_text(
            f"# {args.title.strip()}\n\n"
            f"- **Task ID:** `{task_id}`\n"
            f"- **Workflow:** `{args.workflow}`\n"
            f"- **Status:** Active\n"
            f"- **Created:** {now}\n\n"
            "## Objective\n\n"
            f"{args.description.strip()}\n\n"
            "## Acceptance criteria\n\n"
            "- [ ] The objective is satisfied.\n"
            "- [ ] Relevant tests and quality checks pass.\n"
            "- [ ] Review found no unresolved blocking issue.\n\n"
            "## Constraints and notes\n\n"
            "Add task-specific constraints here before implementation begins.\n",
            encoding="utf-8",
        )
    revision = state.get("revision", 0) + 1
    assignment = orchestrator_assignment(task_id, revision, "Evaluate the new task and select the first useful role.")
    new_state = {
        "$schema": "./SCHEMAS/state.schema.json",
        "protocol_version": PROTOCOL_VERSION,
        "revision": revision,
        "task_id": task_id,
        "task_file": f".ai/TASKS/{task_id}.md",
        "workflow": args.workflow,
        "state": "ORCHESTRATION_REQUIRED",
        "previous_state": None,
        "completed_roles": [],
        "completed_steps": [],
        "active_assignments": [assignment],
        "next_role": "orchestrator",
        "iteration": 0,
        "max_iterations": max_iterations,
        "last_role": None,
        "last_relevant_commit": git_head(root),
        "reason": "A new task requires workflow selection and an initial orchestration decision.",
        "summary": f"Task {task_id} was created; no implementation work has started.",
        "blocked": None,
        "updated_at": now,
    }
    errors = validate_state(root, new_state)
    if errors:
        raise ProtocolError("Cannot start task:\n- " + "\n- ".join(errors))
    write_json(root / ".ai" / "STATE.json", new_state)
    write_json(root / ".ai" / "DECISION.json", initial_decision(task_id, args.workflow, revision))
    refresh_context(root, new_state, reset_notes=True)
    print(f"Started task {task_id} with workflow {args.workflow} at revision {revision}.")


def decision_errors(root: Path, state: dict[str, Any], decision: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    required = {
        "protocol_version",
        "decision_id",
        "task_id",
        "based_on_revision",
        "workflow",
        "state",
        "assignments",
        "iteration",
        "reason",
        "summary",
        "expected_outcome",
        "mark_steps_completed",
        "created_at",
    }
    missing = required - decision.keys()
    if missing:
        return ["Decision missing fields: " + ", ".join(sorted(missing))]
    if decision["protocol_version"] != PROTOCOL_VERSION:
        errors.append(f"decision protocol_version must be {PROTOCOL_VERSION}")
    if not isinstance(decision["decision_id"], str) or not ASSIGNMENT_RE.fullmatch(decision["decision_id"]):
        errors.append("decision_id must contain 3-128 safe characters")
    if decision["task_id"] != state["task_id"]:
        errors.append("decision task_id must match STATE.json")
    if state["workflow"] != "auto" and decision["workflow"] != state["workflow"]:
        errors.append("decision workflow must match STATE.json after intake selection")
    if decision["based_on_revision"] != state["revision"]:
        errors.append(f"stale decision: based_on_revision is {decision['based_on_revision']}, current revision is {state['revision']}")
    if state["state"] != "ORCHESTRATION_REQUIRED":
        errors.append("decisions can only be applied while ORCHESTRATION_REQUIRED")
    elif len(state["active_assignments"]) != 1 or state["active_assignments"][0].get("role") != "orchestrator":
        errors.append("current orchestrator assignment is missing")
    if not isinstance(decision["workflow"], str):
        errors.append("decision workflow must be a string")
        return errors
    workflow = load_workflow(root, decision["workflow"])
    target = decision["state"]
    phase = workflow["phases"].get(target)
    if phase is None:
        errors.append(f"decision state {target!r} is not a phase in workflow {state['workflow']}")
    source = state["previous_state"] or "START"
    if target not in workflow["transitions"].get(source, []):
        errors.append(f"workflow transition {source} -> {target} is not allowed")
    assignments = decision["assignments"]
    if not isinstance(assignments, list):
        errors.append("decision assignments must be an array")
        assignments = []
    identifiers: set[str] = set()
    for index, assignment in enumerate(assignments):
        for error in assignment_errors(assignment, pending=False):
            errors.append(f"decision assignments[{index}]: {error}")
        if isinstance(assignment, dict):
            identifier = assignment.get("id")
            if identifier in identifiers:
                errors.append(f"duplicate assignment id: {identifier}")
            if isinstance(identifier, str):
                identifiers.add(identifier)
            if phase and assignment.get("role") not in phase.get("allowed_roles", []):
                errors.append(f"role {assignment.get('role')} is not allowed in phase {target}")
    terminal = bool(phase and phase.get("terminal"))
    if terminal and assignments:
        errors.append(f"terminal phase {target} cannot contain assignments")
    if phase and not terminal and not assignments:
        errors.append(f"active phase {target} requires at least one assignment")
    max_parallel = load_config(root)["dispatch"]["max_parallel_assignments"]
    if len(assignments) > max_parallel:
        errors.append(f"decision exceeds max_parallel_assignments ({max_parallel})")
    iteration = decision["iteration"]
    if not isinstance(iteration, int):
        errors.append("decision iteration must be an integer")
    else:
        if iteration < state["iteration"] or iteration > state["iteration"] + 1:
            errors.append("decision iteration may stay unchanged or increase by exactly one")
        if iteration > state["max_iterations"]:
            errors.append("decision exceeds max_iterations; complete, block, or request human review")
        if state["iteration"] == 0 and not terminal and iteration != 1:
            errors.append("the first active workflow decision must set iteration to 1")
        if target == source and not terminal and iteration != state["iteration"] + 1:
            errors.append("repeating the same phase requires a new iteration")
    for field in ("reason", "summary"):
        if not isinstance(decision[field], str) or not decision[field].strip():
            errors.append(f"decision {field} must be non-empty")
    if not isinstance(decision["mark_steps_completed"], list) or not all(
        isinstance(item, str) and item.strip() for item in decision["mark_steps_completed"]
    ):
        errors.append("mark_steps_completed must be a string array")
    return errors


def unique_history_path(root: Path, task_id: str, suffix: str) -> Path:
    directory = root / ".ai" / "HISTORY" / task_id
    directory.mkdir(parents=True, exist_ok=True)
    base = directory / f"{timestamp_slug()}-{suffix}.json"
    if not base.exists():
        return base
    for number in range(2, 1000):
        candidate = directory / f"{timestamp_slug()}-{suffix}-{number}.json"
        if not candidate.exists():
            return candidate
    raise ProtocolError("Could not allocate a unique history filename")


def safe_slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    return cleaned[:80] or "record"


def apply_decision(args: argparse.Namespace, root: Path) -> None:
    state = load_state(root)
    decision_path = (root / args.file).resolve() if not Path(args.file).is_absolute() else Path(args.file)
    decision = read_json(decision_path)
    errors = decision_errors(root, state, decision)
    if errors:
        raise ProtocolError("Decision rejected:\n- " + "\n- ".join(errors))
    now = utc_now()
    target = decision["state"]
    assignments = [dict(item, status="pending") for item in decision["assignments"]]
    completed_roles = list(state["completed_roles"])
    completed_roles.append("orchestrator")
    completed_steps = list(state["completed_steps"])
    for step in decision["mark_steps_completed"]:
        if step not in completed_steps:
            completed_steps.append(step)
    previous = state["previous_state"]
    new_state = copy.deepcopy(state)
    new_state.update(
        {
            "revision": state["revision"] + 1,
            "workflow": decision["workflow"],
            "state": target,
            "previous_state": previous,
            "completed_roles": completed_roles,
            "completed_steps": completed_steps,
            "active_assignments": assignments,
            "next_role": assignments[0]["role"] if assignments else None,
            "iteration": decision["iteration"],
            "last_role": "orchestrator",
            "last_relevant_commit": args.commit or git_head(root),
            "reason": decision["reason"].strip(),
            "summary": decision["summary"].strip(),
            "blocked": {
                "reason": decision["reason"].strip(),
                "requested_action": decision.get("expected_outcome"),
            }
            if target in {"BLOCKED", "HUMAN_REVIEW_REQUIRED"}
            else None,
            "updated_at": now,
        }
    )
    errors = validate_state(root, new_state)
    if errors:
        raise ProtocolError("Decision produced invalid state:\n- " + "\n- ".join(errors))
    decision["created_at"] = decision.get("created_at") or now
    history = {
        **decision,
        "$schema": "../../SCHEMAS/decision.schema.json",
        "kind": "orchestrator_decision",
        "applied_revision": new_state["revision"],
    }
    history_path = unique_history_path(root, state["task_id"], f"orchestrator-{safe_slug(decision['decision_id'])}")
    write_json(history_path, history)
    write_json(root / ".ai" / "DECISION.json", decision)
    write_json(root / ".ai" / "STATE.json", new_state)
    refresh_context(root, new_state)
    print(
        f"Applied decision {decision['decision_id']}: {target}; "
        f"{len(assignments)} assignment(s); revision {new_state['revision']}."
    )


def complete_assignment(args: argparse.Namespace, root: Path) -> None:
    state = load_state(root)
    if state["task_id"] is None or state["state"] in TERMINAL_STATES:
        raise ProtocolError("There is no active assignment to complete")
    if args.expected_revision is not None and args.expected_revision != state["revision"]:
        raise ProtocolError(f"stale result: expected revision {args.expected_revision}, current revision is {state['revision']}")
    assignment = next((item for item in state["active_assignments"] if item.get("id") == args.assignment_id), None)
    if assignment is None:
        raise ProtocolError(f"Assignment is not pending: {args.assignment_id}")
    if assignment["role"] == "orchestrator":
        raise ProtocolError("The orchestrator must use apply-decision, not complete-assignment")
    if not args.summary.strip():
        raise ProtocolError("Result summary must be non-empty")
    details = None
    if args.details_file:
        details_path = Path(args.details_file)
        if not details_path.is_absolute():
            details_path = root / details_path
        try:
            details = details_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError as exc:
            raise ProtocolError(f"Details file does not exist: {args.details_file}") from exc
    now = utc_now()
    result = {
        "$schema": "../../SCHEMAS/result.schema.json",
        "protocol_version": PROTOCOL_VERSION,
        "kind": "agent_result",
        "task_id": state["task_id"],
        "workflow": state["workflow"],
        "assignment_id": assignment["id"],
        "role": assignment["role"],
        "state_revision_started": args.started_revision,
        "state_revision_recorded": state["revision"] + 1,
        "status": args.status,
        "summary": args.summary.strip(),
        "details": details,
        "checks": args.check,
        "artifacts": args.artifact,
        "commit": args.commit or git_head(root),
        "completed_at": now,
    }
    history_path = unique_history_path(
        root,
        state["task_id"],
        f"{assignment['role']}-{safe_slug(assignment['id'])}",
    )
    remaining = [item for item in state["active_assignments"] if item.get("id") != args.assignment_id]
    completed_roles = list(state["completed_roles"])
    if assignment["role"] not in completed_roles or args.status == "succeeded":
        completed_roles.append(assignment["role"])
    completed_steps = list(state["completed_steps"])
    if args.step and args.step not in completed_steps:
        completed_steps.append(args.step)
    new_revision = state["revision"] + 1
    previous_state = state["previous_state"]
    new_state_name = state["state"]
    reason = f"Waiting for {len(remaining)} remaining assignment(s) in {state['state']}."
    if not remaining:
        previous_state = state["state"]
        new_state_name = "ORCHESTRATION_REQUIRED"
        remaining = [
            orchestrator_assignment(
                state["task_id"],
                new_revision,
                "Evaluate the latest agent result(s), repository diff, and checks before choosing the next action.",
            )
        ]
        reason = "All assignments for the phase reported; the orchestrator must evaluate the results."
    new_state = copy.deepcopy(state)
    new_state.update(
        {
            "revision": new_revision,
            "state": new_state_name,
            "previous_state": previous_state,
            "completed_roles": completed_roles,
            "completed_steps": completed_steps,
            "active_assignments": remaining,
            "next_role": remaining[0]["role"] if remaining else None,
            "last_role": assignment["role"],
            "last_relevant_commit": args.commit or git_head(root),
            "reason": reason,
            "summary": args.summary.strip(),
            "blocked": {"reason": args.summary.strip(), "requested_action": "Orchestrator evaluation"}
            if args.status == "blocked"
            else state.get("blocked"),
            "updated_at": now,
        }
    )
    errors = validate_state(root, new_state)
    if errors:
        raise ProtocolError("Result produced invalid state:\n- " + "\n- ".join(errors))
    write_json(history_path, result)
    write_json(root / ".ai" / "STATE.json", new_state)
    refresh_context(root, new_state)
    print(
        f"Recorded {args.status} result for {assignment['id']} at {history_path.relative_to(root)}; "
        f"revision {new_revision}."
    )


def render_dispatches(args: argparse.Namespace, root: Path) -> None:
    state = load_state(root)
    errors = validate_state(root, state)
    if errors:
        raise ProtocolError("Cannot dispatch invalid state:\n- " + "\n- ".join(errors))
    events = []
    for assignment in state["active_assignments"]:
        role = assignment["role"]
        payload = {
            "$schema": ".ai/SCHEMAS/dispatch.schema.json",
            "protocol_version": PROTOCOL_VERSION,
            "request_id": f"{state['task_id']}:{assignment['id']}",
            "task_id": state["task_id"],
            "workflow": state["workflow"],
            "state": state["state"],
            "state_revision": state["revision"],
            "assignment_id": assignment["id"],
            "role": role,
            "scope": assignment["scope"],
            "instructions": assignment["instructions"],
            "acceptance_criteria": assignment["acceptance_criteria"],
            "repository": args.repository,
            "ref": args.ref,
            "sha": args.sha,
            "task_file": state["task_file"],
            "role_file": f".ai/ROLES/{role}.md",
            "state_file": ".ai/STATE.json",
            "context_file": ".ai/CONTEXT.md",
        }
        events.append({"event_type": "ai_role_requested", "client_payload": payload})
    print(json.dumps(events, ensure_ascii=False))


def event_payload(event_path: Path) -> dict[str, Any]:
    event = read_json(event_path)
    payload = event.get("client_payload")
    if not isinstance(payload, dict):
        raise ProtocolError("Event has no client_payload object")
    return payload


def verify_request(args: argparse.Namespace, root: Path) -> None:
    payload = event_payload(Path(args.event))
    state = load_state(root)
    if payload.get("protocol_version") != PROTOCOL_VERSION:
        raise ProtocolError("Dispatch protocol version does not match")
    if payload.get("task_id") != state["task_id"]:
        raise ProtocolError("Stale dispatch: task id no longer matches")
    assignment = next(
        (item for item in state["active_assignments"] if item.get("id") == payload.get("assignment_id")),
        None,
    )
    if assignment is None:
        raise ProtocolError("Stale dispatch: assignment is no longer pending")
    if payload.get("role") != assignment["role"]:
        raise ProtocolError("Dispatch role does not match the pending assignment")
    expected_request_id = f"{state['task_id']}:{assignment['id']}"
    if payload.get("request_id") != expected_request_id:
        raise ProtocolError("Dispatch request_id is invalid")
    print(f"Verified request {expected_request_id} for role {assignment['role']}.")


def request_markdown(args: argparse.Namespace, root: Path) -> None:
    payload = event_payload(Path(args.event))
    criteria = "\n".join(f"- {item}" for item in payload.get("acceptance_criteria", []))
    print(
        f"<!-- ai-request-id: {payload.get('request_id')} -->\n"
        f"# AI role handoff: {payload.get('role')}\n\n"
        f"No agent webhook is configured, so this request needs a manually started agent or human.\n\n"
        f"- **Task:** `{payload.get('task_id')}`\n"
        f"- **Workflow/state:** `{payload.get('workflow')}` / `{payload.get('state')}`\n"
        f"- **Assignment:** `{payload.get('assignment_id')}`\n"
        f"- **Repository revision:** `{payload.get('sha')}` (state revision `{payload.get('state_revision')}`)\n\n"
        f"## Scope\n\n{payload.get('scope')}\n\n"
        f"## Instructions\n\n{payload.get('instructions')}\n\n"
        f"## Acceptance criteria\n\n{criteria}\n\n"
        "## Bootstrap\n\n"
        f"Start with `{payload.get('role_file')}`, then follow `.ai/AGENTS.md` and the bootstrap order. "
        "Record the result through `scripts/ai/protocol.py`; do not merely close this issue.\n"
    )


def show_status(args: argparse.Namespace, root: Path) -> None:
    state = load_state(root)
    if args.json:
        print(json.dumps(state, indent=2, ensure_ascii=False))
        return
    print(f"Task:       {state['task_id'] or '-'}")
    print(f"Workflow:   {state['workflow'] or '-'}")
    print(f"State:      {state['state']}")
    print(f"Revision:   {state['revision']}")
    print(f"Iteration:  {state['iteration']}/{state['max_iterations']}")
    print(f"Next role:  {state['next_role'] or '-'}")
    print(f"Assignments:{len(state['active_assignments']):>3}")
    print(f"Summary:    {state['summary']}")


def run_validate(args: argparse.Namespace, root: Path) -> None:
    errors = validate_repository(root)
    if args.json:
        print(json.dumps({"valid": not errors, "errors": errors}, indent=2))
    elif errors:
        print("AI protocol validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
    else:
        print("AI protocol validation passed.")
    if errors:
        raise SystemExit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the repository-backed multi-agent protocol")
    parser.add_argument("--root", help="Repository root (defaults to the script's repository)")
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="Validate protocol files and state invariants")
    validate.add_argument("--json", action="store_true")
    validate.set_defaults(handler=run_validate)

    status = commands.add_parser("status", help="Show the current persistent state")
    status.add_argument("--json", action="store_true")
    status.set_defaults(handler=show_status)

    start = commands.add_parser("start-task", help="Create and activate a task")
    start.add_argument("--id", required=True)
    start.add_argument("--title", required=True)
    start.add_argument(
        "--workflow",
        required=True,
        choices=("auto", "cleanup", "feature", "bugfix", "refactor", "security"),
        help="Use 'auto' to let the first orchestrator decision select the workflow",
    )
    start.add_argument("--description", required=True)
    start.add_argument("--max-iterations", type=int)
    start.add_argument("--reuse", action="store_true", help="Reactivate an existing task file")
    start.set_defaults(handler=start_task)

    apply_cmd = commands.add_parser("apply-decision", help="Validate and apply an orchestrator decision")
    apply_cmd.add_argument("--file", default=".ai/DECISION.json")
    apply_cmd.add_argument("--commit", help="Relevant commit SHA (defaults to HEAD)")
    apply_cmd.set_defaults(handler=apply_decision)

    complete = commands.add_parser("complete-assignment", help="Record a non-orchestrator result and hand back")
    complete.add_argument("--assignment-id", required=True)
    complete.add_argument("--status", required=True, choices=("succeeded", "failed", "blocked"))
    complete.add_argument("--summary", required=True)
    complete.add_argument("--details-file")
    complete.add_argument("--check", action="append", default=[], help="A check and its outcome; repeatable")
    complete.add_argument("--artifact", action="append", default=[], help="A relevant repository path; repeatable")
    complete.add_argument("--step", help="A concise completed-step label")
    complete.add_argument("--commit", help="Relevant commit SHA (defaults to HEAD)")
    complete.add_argument("--started-revision", type=int, help="State revision read when work began")
    complete.add_argument("--expected-revision", type=int, help="Reject if STATE.json has moved since this revision")
    complete.set_defaults(handler=complete_assignment)

    dispatches = commands.add_parser("render-dispatches", help="Render repository_dispatch event bodies")
    dispatches.add_argument("--repository", required=True)
    dispatches.add_argument("--ref", required=True)
    dispatches.add_argument("--sha", required=True)
    dispatches.set_defaults(handler=render_dispatches)

    verify = commands.add_parser("verify-request", help="Reject stale or invalid repository_dispatch requests")
    verify.add_argument("--event", required=True)
    verify.set_defaults(handler=verify_request)

    markdown = commands.add_parser("request-markdown", help="Render a manual handoff issue from a dispatch event")
    markdown.add_argument("--event", required=True)
    markdown.set_defaults(handler=request_markdown)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    root = repository_root(args.root)
    try:
        args.handler(args, root)
    except ProtocolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
