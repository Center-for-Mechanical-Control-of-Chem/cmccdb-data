"""Exercise the public protocol with separate real stdio server processes."""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from contextlib import AsyncExitStack
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from cmccdb_extraction.responses import decode_inspection_response

PAPERS = os.environ.get("CMCCDB_TEST_PAPERS")
POPLER = os.environ.get("CMCCDB_POPPLER_BIN")
TEST_ROOT = os.environ.get("CMCCDB_TEST_WORK_ROOT", str(Path.cwd()))


def server_parameters(work_root):
    args = ["-m", "cmccdb_extraction.server", "--paper-root", PAPERS, "--work-root", str(work_root)]
    if POPLER:
        args += ["--poppler-bin", POPLER]
    if os.environ.get("CMCCDB_XLSX_NODE"):
        args += ["--node", os.environ["CMCCDB_XLSX_NODE"]]
    return StdioServerParameters(command=sys.executable, args=args, env=dict(os.environ))


async def connect(stack, work_root):
    read, write = await stack.enter_async_context(stdio_client(server_parameters(work_root)))
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    return session


async def call(session, name, **arguments):
    result = await session.call_tool(name, arguments)
    if result.is_error:
        raise ValueError("; ".join(c.text for c in result.content if hasattr(c, "text")))
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(next(c.text for c in result.content if hasattr(c, "text")))


@unittest.skipUnless(PAPERS, "Set CMCCDB_TEST_PAPERS for protocol integration")
class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_independent_connections_shared_state_and_restart(self):
        with tempfile.TemporaryDirectory(prefix="mcp-tests-", dir=TEST_ROOT) as directory:
            async with AsyncExitStack() as stack:
                manager = await connect(stack, directory)
                tools = await manager.list_tools()
                self.assertIn("submit_extraction", {t.name for t in tools.tools})
                self.assertIn("adjudicate_claims", {t.name for t in tools.tools})
                self.assertEqual({t.name for t in tools.tools if 'compressed' in t.input_schema.get('properties',{})},
                                 {'inspect_consensus','validate_extraction'})
                capabilities = await call(manager, "get_capabilities")
                self.assertTrue(capabilities["pdf_ready"])
                self.assertTrue(capabilities["schema_string_patch_present"])
                prompts = await manager.list_prompts()
                self.assertIn("extraction_worker", {p.name for p in prompts.prompts})
                papers = await call(manager, "list_papers")
                self.assertGreaterEqual(papers["total"], 8)
                manifest = await call(manager, "create_run", paper="d6mr00048g.pdf",
                    curator_name="Protocol fixture", curator_email="fixture@example.invalid")
                run = manifest["run_id"]
                sid = manifest["sources"][0]["source_id"]
                found = await call(manager, "search_evidence", run_id=run, query="NaOH milled 30")
                line = found["matches"][0]
                image = await manager.call_tool("view_page", dict(run_id=run, source_id=sid, page=3,
                    bbox=[40.0, 45.0, 295.0, 337.0], dpi=80))
                self.assertFalse(image.is_error)
                self.assertTrue(any(c.type == "image" for c in image.content))
                plan = dict(experiments=[dict(key="example", label="Frequency fixture",
                    scope="One condition; no complete-paper coverage", evidence_ids=[line["evidence_id"]])],
                    tasks=[dict(task_id="conditions", experiment_keys=["example"],
                        allowed_paths=["/conditions/mechanochemistry/frequency"],
                        evidence_ids=[line["evidence_id"]], instructions="Extract the frequency", required_workers=2)],
                    source_search_complete=False)
                await call(manager, "install_search_plan", run_id=run, plan=plan)
                worker = await connect(stack, directory)
                for connection, identity in [(manager, "fixture-a"), (worker, "fixture-b")]:
                    packet = await call(connection, "lease_extraction_task", run_id=run, worker_id=identity,
                        model_revision="recorded-fixture-v1", worker_kind="fixture")
                    self.assertTrue(packet["available"])
                    self.assertNotIn("candidates", packet)
                    await call(connection, "submit_extraction", run_id=run, task_id="conditions",
                        worker_id=identity, lease_token=packet["lease_token"], submission=dict(claims=[dict(
                            experiment_key="example", path="/conditions/mechanochemistry/frequency",
                            value=dict(value=30, units="HERTZ"), source_value="30 Hz", basis="reported",
                            evidence=[dict(evidence_id=line["evidence_id"], quote="30 Hz")])]))
                consensus = await call(manager, "inspect_consensus", run_id=run)
                self.assertEqual(consensus["fields"][0]["status"], "agreed")
                packed=await call(manager,"inspect_consensus",run_id=run,compressed=True)
                self.assertEqual(decode_inspection_response(packed),consensus)
                field=consensus["fields"][0]
                chosen=dict(experiment_key="example",path=field["path"],claim_id=field["candidates"][0]["claim_id"],actor="Protocol fixture",reason="Explicit existing candidate for atomic protocol regression.")
                accepted=await call(manager,"adjudicate_claims",run_id=run,decisions=[chosen])
                self.assertEqual(accepted,dict(adjudicated=1,atomic=True))
                before=await call(manager,"get_audit_events",run_id=run)
                failed=await manager.call_tool("adjudicate_claims",dict(run_id=run,decisions=[{**chosen,"claim_id":"unknown"}]))
                self.assertTrue(failed.is_error)
                after=await call(manager,"get_audit_events",run_id=run)
                self.assertEqual(before,after)
                invalid = await manager.call_tool("read_evidence", dict(run_id=run, evidence_ids=["unknown"]))
                self.assertTrue(invalid.is_error)
                self.assertIn("Unknown source ID", " ".join(c.text for c in invalid.content if hasattr(c, "text")))
            async with AsyncExitStack() as restarted:
                session = await connect(restarted, directory)
                state = await call(session, "get_run_status", run_id=run)
                self.assertEqual(state["claim_count"], 2)
                self.assertTrue(state["tasks"][0]["completed"])
                history = await call(session, "get_audit_events", run_id=run)
                self.assertEqual(sum(e["kind"] == "claims_submitted" for e in history["events"]), 2)

    async def test_paper_si_reference_replay_and_export_over_mcp(self):
        if not (Path(PAPERS) / "d6mr00048g1_suppl.pdf").is_file():
            self.skipTest("Reference SI is not present")
        sys.path.insert(0, str(Path(__file__).parents[1] / "examples"))
        from replay_carbonates import replay
        with tempfile.TemporaryDirectory(prefix="mcp-reference-tests-", dir=TEST_ROOT) as directory:
            async with AsyncExitStack() as stack:
                session = await connect(stack, directory)
                result = await replay(session, export=bool(os.environ.get("CMCCDB_XLSX_NODE")))
                self.assertTrue(result["validation"]["valid"])
                self.assertTrue(result["validation"]["fixture_run"])
                self.assertFalse(result["validation"]["ready_for_contribution"])
                run = result["manifest"]["run_id"]
                assembled = await call(session, "validate_extraction", run_id=run)
                packed=await call(session,"validate_extraction",run_id=run,compressed=True)
                self.assertEqual(decode_inspection_response(packed),assembled)
                reactions = assembled["dataset"]["reactions"]
                self.assertEqual([r["conditions"]["mechanochemistry"]["duration"]["value"] for r in reactions], [3600, 1800, 1800])
                self.assertEqual([r["outcomes"][0]["products"][0]["measurements"][0]["percentage"]["value"] for r in reactions], [84, 22, 78])
                blocked = await session.call_tool("export_workbook", dict(run_id=run))
                self.assertTrue(blocked.is_error)
                if "export" in result:
                    self.assertTrue(result["export"]["round_trip_verified"])
                    self.assertEqual(result["export"]["reactions"], 3)
                    import zipfile
                    with zipfile.ZipFile(result["export"]["workbook"]) as archive:
                        self.assertEqual(len([n for n in archive.namelist() if n.startswith("xl/media/")]), 2)
                    auxiliary=Path(directory)/run/'review-provenance.json.gz'
                    auxiliary.write_bytes(b'Explicit fixture auxiliary snapshot')
                    exported=await call(session,'export_workbook',run_id=run,allow_partial=True,
                                        auxiliary_files=[str(auxiliary)])
                    self.assertIn(auxiliary.name,exported['auxiliary_files'])
                    import base64
                    resource=await session.read_resource(exported['resources'][auxiliary.name])
                    self.assertEqual(base64.b64decode(resource.contents[0].blob),auxiliary.read_bytes())
                    unknown=exported['resources'][auxiliary.name].replace(auxiliary.name,'unlisted.json.gz')
                    with self.assertRaises(Exception):await session.read_resource(unknown)


if __name__ == "__main__":
    unittest.main()
