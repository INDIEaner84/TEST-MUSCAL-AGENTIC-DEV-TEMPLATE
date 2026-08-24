from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

REPOSITORY = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY / "scripts" / "ai"))

import protocol  # noqa: E402


class ProtocolTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        shutil.copytree(REPOSITORY / ".ai", self.root / ".ai")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def start_task(self) -> dict:
        protocol.start_task(
            SimpleNamespace(
                id="feature-001",
                title="Example feature",
                workflow="feature",
                description="Implement an observable example without depending on chat memory.",
                max_iterations=None,
                reuse=False,
            ),
            self.root,
        )
        return protocol.load_state(self.root)

    def analysis_decision(self, revision: int, iteration: int = 1) -> dict:
        return {
            "$schema": "./SCHEMAS/decision.schema.json",
            "protocol_version": "1.0",
            "decision_id": "feature-001-analysis-1",
            "task_id": "feature-001",
            "based_on_revision": revision,
            "workflow": "feature",
            "state": "ARCHITECTURE_REQUIRED",
            "assignments": [
                {
                    "id": "feature-001:analyzer:architecture-1",
                    "role": "analyzer",
                    "scope": "Relevant architecture",
                    "instructions": "Inspect current behavior and produce a bounded implementation plan.",
                    "acceptance_criteria": ["Findings cite files", "Plan names validation commands"],
                }
            ],
            "iteration": iteration,
            "reason": "Architecture evidence is required before implementation.",
            "summary": "The task is ready for architecture analysis.",
            "expected_outcome": "A repository-specific implementation plan.",
            "mark_steps_completed": ["Initial orchestration"],
            "created_at": None,
        }

    def write_decision(self, value: dict) -> Path:
        path = self.root / "decision-under-test.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_template_state_validates(self) -> None:
        self.assertEqual([], protocol.validate_repository(self.root))

    def test_task_decision_result_round_trip(self) -> None:
        state = self.start_task()
        self.assertEqual("ORCHESTRATION_REQUIRED", state["state"])
        self.assertEqual("orchestrator", state["next_role"])

        decision_path = self.write_decision(self.analysis_decision(state["revision"]))
        protocol.apply_decision(SimpleNamespace(file=str(decision_path), commit="abc123"), self.root)
        state = protocol.load_state(self.root)
        self.assertEqual("ARCHITECTURE_REQUIRED", state["state"])
        self.assertEqual("analyzer", state["next_role"])
        self.assertEqual(1, state["iteration"])

        context_path = self.root / ".ai" / "CONTEXT.md"
        context_path.write_text(context_path.read_text() + "- Preserve this architecture risk.\n")
        assignment_id = state["active_assignments"][0]["id"]
        protocol.complete_assignment(
            SimpleNamespace(
                assignment_id=assignment_id,
                status="succeeded",
                summary="Architecture findings and a bounded plan were recorded.",
                details_file=None,
                check=["repository inspection: passed"],
                artifact=["src/example.py"],
                step="Architecture analyzed",
                commit="def456",
                started_revision=state["revision"],
                expected_revision=state["revision"],
            ),
            self.root,
        )
        final_state = protocol.load_state(self.root)
        self.assertEqual("ORCHESTRATION_REQUIRED", final_state["state"])
        self.assertEqual("ARCHITECTURE_REQUIRED", final_state["previous_state"])
        self.assertEqual("orchestrator", final_state["next_role"])
        self.assertIn("Architecture analyzed", final_state["completed_steps"])
        context = context_path.read_text()
        self.assertIn("state revision 3", context)
        self.assertIn("Preserve this architecture risk", context)
        self.assertEqual([], protocol.validate_repository(self.root))
        self.assertEqual(2, len(list((self.root / ".ai" / "HISTORY" / "feature-001").glob("*.json"))))

    def test_auto_intake_allows_orchestrator_to_select_workflow(self) -> None:
        protocol.start_task(
            SimpleNamespace(
                id="feature-001",
                title="Example feature",
                workflow="auto",
                description="Let the orchestrator select the matching workflow.",
                max_iterations=None,
                reuse=False,
            ),
            self.root,
        )
        state = protocol.load_state(self.root)
        self.assertEqual("auto", state["workflow"])
        decision_path = self.write_decision(self.analysis_decision(state["revision"]))
        protocol.apply_decision(SimpleNamespace(file=str(decision_path), commit=None), self.root)
        self.assertEqual("feature", protocol.load_state(self.root)["workflow"])

    def test_stale_decision_is_rejected(self) -> None:
        state = self.start_task()
        decision = self.analysis_decision(state["revision"] - 1)
        errors = protocol.decision_errors(self.root, state, decision)
        self.assertTrue(any("stale decision" in error for error in errors))

    def test_same_phase_requires_new_iteration(self) -> None:
        state = self.start_task()
        decision_path = self.write_decision(self.analysis_decision(state["revision"]))
        protocol.apply_decision(SimpleNamespace(file=str(decision_path), commit=None), self.root)
        state = protocol.load_state(self.root)
        protocol.complete_assignment(
            SimpleNamespace(
                assignment_id=state["active_assignments"][0]["id"],
                status="succeeded",
                summary="First analysis completed.",
                details_file=None,
                check=[],
                artifact=[],
                step=None,
                commit=None,
                started_revision=state["revision"],
                expected_revision=state["revision"],
            ),
            self.root,
        )
        state = protocol.load_state(self.root)
        repeated = self.analysis_decision(state["revision"], iteration=1)
        repeated["decision_id"] = "feature-001-analysis-repeat"
        errors = protocol.decision_errors(self.root, state, repeated)
        self.assertTrue(any("repeating the same phase" in error for error in errors))

    def test_dispatch_request_is_idempotently_verifiable(self) -> None:
        state = self.start_task()
        output_path = self.root / "event.json"
        payload = {
            "client_payload": {
                "protocol_version": "1.0",
                "request_id": f"feature-001:{state['active_assignments'][0]['id']}",
                "task_id": "feature-001",
                "assignment_id": state["active_assignments"][0]["id"],
                "role": "orchestrator",
            }
        }
        output_path.write_text(json.dumps(payload), encoding="utf-8")
        protocol.verify_request(SimpleNamespace(event=str(output_path)), self.root)


if __name__ == "__main__":
    unittest.main()
