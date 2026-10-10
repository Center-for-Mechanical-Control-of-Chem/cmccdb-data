"""Routing behavior and public protocol checks for external model dispatch."""
import tempfile
import unittest
from contextlib import AsyncExitStack

from pydantic import ValidationError
from cmccdb_extraction.contracts import Task
from cmccdb_extraction.dispatch import DISPATCH_INSTRUCTIONS, recommend_dispatch
from test_agent_policy import connect, call, prompt_text, PAPERS, TEST_ROOT


def task(path="/conditions/mechanochemistry/frequency", complexity="routine", **overrides):
    entry = dict(task_id="conditions", experiment_keys=["example"], allowed_paths=[path],
                evidence_ids=["example:p0001:l0001"], instructions="Extract explicit reported frequency",
                dispatch=dict(complexity=complexity, reason="Clearly labelled numerical value in a source table.",
                              required_capabilities=["text"]))
    entry.update(overrides)
    return entry


class DispatchTests(unittest.TestCase):
    def test_regular_numeric_scope_uses_light_workers_regardless_of_batch_size(self):
        entry = task()
        entry["experiment_keys"] = [f"experiment-{i}" for i in range(100)]
        result = recommend_dispatch(Task.model_validate(entry).model_dump())
        self.assertEqual(result["preferred_worker_role"], "light")
        self.assertEqual(result["required_capabilities"], ["text"])
        self.assertFalse(result["runtime_enforcement"])

    def test_vision_is_separate_from_reasoning_complexity(self):
        result = recommend_dispatch(Task.model_validate(task(modality="vision")).model_dump())
        self.assertEqual(result["preferred_worker_role"], "light")
        self.assertIn("vision", result["required_capabilities"])

    def test_structure_and_broad_compound_scopes_cannot_recommend_light_workers(self):
        for path in ["/inputs", "/outcomes/0", "/inputs/a/components/0",
                     "/inputs/a/components/0/identifiers/0/value",
                     "/inputs/a/components/0/crystal_parameters/a"]:
            with self.subTest(path=path):
                result = recommend_dispatch(Task.model_validate(task(path)).model_dump())
                self.assertEqual(result["assigned_complexity"], "routine")
                self.assertEqual(result["effective_complexity"], "expert")
                self.assertEqual(result["preferred_worker_role"], "expert")
                self.assertTrue(result["scope_guardrails"])
                self.assertIn("chemistry", result["required_capabilities"])

    def test_context_and_legacy_defaults_use_standard_reasoning(self):
        contextual = Task.model_validate(task(complexity="contextual")).model_dump()
        self.assertEqual(recommend_dispatch(contextual)["preferred_worker_role"], "standard")
        del contextual["dispatch"]
        result = recommend_dispatch(contextual)
        self.assertFalse(result["classified"])
        self.assertEqual(result["preferred_worker_role"], "standard")

    def test_dispatch_requires_known_complexity_and_explained_reason(self):
        for changes in [dict(complexity="cheap"), dict(reason=" "),
                        dict(required_capabilities=["invented"]), dict(reason=42)]:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                entry = task()
                entry["dispatch"].update(changes)
                Task.model_validate(entry)


@unittest.skipUnless(PAPERS, "Set CMCCDB_TEST_PAPERS for public protocol checks")
class DispatchMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_manager_plans_routing_before_blind_workers_and_audits_fallback(self):
        with tempfile.TemporaryDirectory(prefix="dispatch-", dir=TEST_ROOT) as directory:
            async with AsyncExitStack() as stack:
                manager, initialized = await connect(stack, directory)
                self.assertIn(DISPATCH_INSTRUCTIONS, initialized.instructions)
                capability = await call(manager, "get_capabilities")
                self.assertEqual(capability["dispatch_policy"]["roles"]["routine"], "light")
                manifest = await call(manager, "create_run", paper="d6mr00048g.pdf")
                run = manifest["run_id"]
                manager_prompt = await manager.get_prompt("extraction_manager", dict(run_id=run))
                self.assertIn(DISPATCH_INSTRUCTIONS, prompt_text(manager_prompt))
                evidence = (await call(manager, "search_evidence", run_id=run, query="NaOH milled 30"))["matches"][0]["evidence_id"]
                entry = task()
                entry["evidence_ids"] = [evidence]
                plan = dict(experiments=[dict(key="example", label="One condition fixture",
                    scope="Explicit frequency only", evidence_ids=[evidence])], tasks=[entry])
                await call(manager, "install_search_plan", run_id=run, plan=plan)
                status = await call(manager, "get_run_status", run_id=run)
                self.assertEqual(status["tasks"][0]["dispatch"]["preferred_worker_role"], "light")
                worker, initialized = await connect(stack, directory)
                self.assertIn(DISPATCH_INSTRUCTIONS, initialized.instructions)
                packet = await call(worker, "lease_extraction_task", run_id=run,
                    worker_id="routing-fixture-a", model_revision="actual-fixture-revision",
                    worker_kind="fixture", dispatch_note="Fixture standard fallback; light role unavailable.")
                self.assertEqual(packet["dispatch"], status["tasks"][0]["dispatch"])
                self.assertNotIn("candidates", packet)
                self.assertIn(DISPATCH_INSTRUCTIONS, packet["instructions"])
                history = await call(manager, "get_audit_events", run_id=run)
                leased = next(e["data"] for e in history["events"] if e["kind"] == "task_leased")
                self.assertEqual(leased["dispatch"], packet["dispatch"])
                self.assertIn("light role unavailable", leased["dispatch_note"])
                self.assertEqual(leased["model_revision"], "actual-fixture-revision")

    async def test_legacy_plan_retry_remains_idempotent_and_advice_is_derived(self):
        with tempfile.TemporaryDirectory(prefix="dispatch-legacy-", dir=TEST_ROOT) as directory:
            async with AsyncExitStack() as stack:
                session, _ = await connect(stack, directory)
                manifest = await call(session, "create_run", paper="d6mr00048g.pdf")
                run = manifest["run_id"]
                line = (await call(session, "search_evidence", run_id=run, query="NaOH milled 30"))["matches"][0]
                entry = task()
                del entry["dispatch"]
                entry["evidence_ids"] = [line["evidence_id"]]
                plan = dict(experiments=[dict(key="example", label="Legacy plan fixture",
                    scope="Frequency only", evidence_ids=[line["evidence_id"]])], tasks=[entry])
                await call(session, "install_search_plan", run_id=run, plan=plan)
                repeated = await call(session, "install_search_plan", run_id=run, plan=plan)
                self.assertTrue(repeated["unchanged"])
                status = await call(session, "get_run_status", run_id=run)
                self.assertFalse(status["tasks"][0]["dispatch"]["classified"])
                self.assertEqual(status["tasks"][0]["dispatch"]["preferred_worker_role"], "standard")
