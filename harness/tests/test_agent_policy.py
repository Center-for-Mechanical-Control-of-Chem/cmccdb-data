"""Verify policy delivery through actual fresh and restarted MCP connections."""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import AsyncExitStack
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from cmccdb_extraction.agent_policy import (
    COMMON_REQUIREMENTS, MANAGER_REQUIREMENTS, WORKER_REQUIREMENTS,
    policy_instructions, policy_metadata,
)
from cmccdb_extraction.pipeline import WORKER_INSTRUCTIONS

PAPERS = os.environ.get("CMCCDB_TEST_PAPERS")
TEST_ROOT = os.environ.get("CMCCDB_TEST_WORK_ROOT", str(Path.cwd()))


async def connect(stack, directory):
    args = ["-m", "cmccdb_extraction.server", "--paper-root", PAPERS,
            "--work-root", str(directory)]
    for key, flag in [("CMCCDB_POPPLER_BIN", "--poppler-bin"),
                      ("CMCCDB_XLSX_NODE", "--node")]:
        if os.environ.get(key):
            args += [flag, os.environ[key]]
    parameters = StdioServerParameters(command=sys.executable, args=args,
                                       env=dict(os.environ))
    reader, writer = await stack.enter_async_context(stdio_client(parameters))
    session = await stack.enter_async_context(ClientSession(reader, writer))
    return session, await session.initialize()


async def call(session, name, **arguments):
    result = await session.call_tool(name, arguments)
    if result.is_error:
        raise ValueError("; ".join(c.text for c in result.content if hasattr(c, "text")))
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(next(c.text for c in result.content if hasattr(c, "text")))


def prompt_text(result):
    return "\n".join(m.content.text for m in result.messages)


class PolicyTests(unittest.TestCase):
    def test_roles_and_independent_metadata_results(self):
        for role, included, excluded in [
            ("worker", WORKER_REQUIREMENTS, MANAGER_REQUIREMENTS),
            ("manager", MANAGER_REQUIREMENTS, WORKER_REQUIREMENTS),
        ]:
            text = policy_instructions(role)
            self.assertEqual(text.count(COMMON_REQUIREMENTS), 1)
            self.assertIn(included, text)
            self.assertNotIn(excluded, text)
            self.assertIn(policy_metadata()["sha256"], text)
        first = policy_metadata()
        first["version"] = "caller mutation"
        self.assertNotEqual(first, policy_metadata())
        with self.assertRaises(ValueError):
            policy_instructions("unknown")


@unittest.skipUnless(PAPERS, "Set CMCCDB_TEST_PAPERS for protocol integration")
class PolicyMCPTests(unittest.IsolatedAsyncioTestCase):
    def check_initialization(self, initialized):
        self.assertIn(policy_instructions("all"), initialized.instructions)

    def check_packet(self, packet):
        self.assertEqual(packet["agent_policy"], policy_metadata())
        self.assertEqual(packet["instructions"], WORKER_INSTRUCTIONS)
        self.assertIn(COMMON_REQUIREMENTS, packet["instructions"])
        self.assertIn(WORKER_REQUIREMENTS, packet["instructions"])
        self.assertNotIn("candidates", packet)

    async def test_new_workers_restart_and_legacy_run_receive_current_policy(self):
        with tempfile.TemporaryDirectory(prefix="agent-policy-", dir=TEST_ROOT) as directory:
            async with AsyncExitStack() as manager_stack:
                manager, initialized = await connect(manager_stack, directory)
                self.check_initialization(initialized)
                capabilities = await call(manager, "get_capabilities")
                self.assertEqual(capabilities["agent_policy"], policy_metadata())
                manifest = await call(manager, "create_run", paper="d6mr00048g.pdf",
                                      curator_name="Policy protocol fixture",
                                      curator_email="fixture@example.invalid")
                self.assertEqual(manifest["agent_policy"], policy_metadata())
                run = manifest["run_id"]
                manager_prompt = prompt_text(await manager.get_prompt(
                    "extraction_manager", dict(run_id=run)))
                self.assertIn(policy_instructions("manager"), manager_prompt)
                found = await call(manager, "search_evidence", run_id=run,
                                   query="NaOH milled 30")
                evidence = found["matches"][0]["evidence_id"]
                plan = dict(experiments=[dict(key="example", label="Policy fixture",
                    scope="One condition only", evidence_ids=[evidence])],
                    tasks=[dict(task_id="conditions", experiment_keys=["example"],
                        allowed_paths=["/conditions/mechanochemistry/frequency"],
                        evidence_ids=[evidence], instructions="Extract the frequency",
                        required_workers=2)], source_search_complete=False)
                await call(manager, "install_search_plan", run_id=run, plan=plan)

                # Simulate a persisted run made before policy metadata existed.
                # This changes only this disposable fixture store.
                with sqlite3.connect(Path(directory) / "harness.sqlite3") as con:
                    legacy = dict(manifest)
                    del legacy["agent_policy"]
                    con.execute("UPDATE runs SET manifest=? WHERE id=?",
                                (json.dumps(legacy), run))

                async with AsyncExitStack() as first_worker_stack:
                    worker, initialized = await connect(first_worker_stack, directory)
                    self.check_initialization(initialized)
                    self.assertEqual(prompt_text(await worker.get_prompt(
                        "extraction_worker")), WORKER_INSTRUCTIONS)
                    first = await call(worker, "lease_extraction_task", run_id=run,
                        worker_id="policy-worker-a", model_revision="fixture-policy-v1",
                        worker_kind="fixture")
                    self.assertTrue(first["available"])
                    self.check_packet(first)

                async with AsyncExitStack() as restarted_stack:
                    restarted, initialized = await connect(restarted_stack, directory)
                    self.check_initialization(initialized)
                    repeated = await call(restarted, "lease_extraction_task", run_id=run,
                        worker_id="policy-worker-a", model_revision="fixture-policy-v1",
                        worker_kind="fixture")
                    self.check_packet(repeated)
                    self.assertEqual(first["lease_token"], repeated["lease_token"])

                async with AsyncExitStack() as second_worker_stack:
                    worker, initialized = await connect(second_worker_stack, directory)
                    self.check_initialization(initialized)
                    second = await call(worker, "lease_extraction_task", run_id=run,
                        worker_id="policy-worker-b", model_revision="fixture-policy-v1",
                        worker_kind="fixture")
                    self.assertTrue(second["available"])
                    self.check_packet(second)
                    self.assertNotEqual(first["lease_token"], second["lease_token"])

                no_work = await call(manager, "lease_extraction_task", run_id=run,
                    worker_id="policy-worker-c", model_revision="fixture-policy-v1",
                    worker_kind="fixture")
                self.assertFalse(no_work["available"])
                self.check_packet(no_work)
                audit = await call(manager, "get_audit_events", run_id=run)
                leases = [e for e in audit["events"] if e["kind"] == "task_leased"]
                self.assertEqual(len(leases), 3)
                for event in leases:
                    self.assertEqual(event["data"]["agent_policy"], policy_metadata())
                    self.assertEqual(event["data"]["worker_instructions"], WORKER_INSTRUCTIONS)
                runs = await call(manager, "list_runs")
                saved = next(m for m in runs["runs"] if m["run_id"] == run)
                self.assertNotIn("agent_policy", saved)


if __name__ == "__main__":
    unittest.main()
