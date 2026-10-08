"""Behavioral tests. PDF integration uses the user's actual paper corpus when configured."""

import copy
import json
import os
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from cmccdb_extraction.contracts import Claim
from cmccdb_extraction.pipeline import Pipeline
from cmccdb_extraction.schema import catalog, field_at, normalize_claim, set_pointer, dataset_from_json
from cmccdb_extraction.documents import check_bbox, index_pdf
from cmccdb_extraction.export import spreadsheet_rows
from cmccdb_schema.dataset_constructor import DatasetConstructor
from cmccdb_schema.proto import reaction_pb2
from google.protobuf.json_format import ParseDict


PAPERS = os.environ.get("CMCCDB_TEST_PAPERS")
POPPER = os.environ.get("CMCCDB_POPPLER_BIN")
TEST_ROOT = os.environ.get("CMCCDB_TEST_WORK_ROOT", str(Path.cwd()))


class SchemaTests(unittest.TestCase):
    def test_boolean_type_is_not_coerced(self):
        with self.assertRaises(ValueError):
            normalize_claim("/inputs/a/components/0/is_limiting", 1)

    def test_unknown_field_and_enum_rejected(self):
        for path, value in [("/conditions/imaginary", 30),
                            ("/conditions/mechanochemistry/type", "EMM")]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                normalize_claim(path, value)

    def test_incompatible_units_rejected(self):
        with self.assertRaises(ValueError):
            normalize_claim("/conditions/mechanochemistry/frequency", dict(value=30, units="g"))

    def test_equivalent_units_have_same_normalized_claim(self):
        path = "/conditions/mechanochemistry/duration"
        a = normalize_claim(path, dict(value=60, units="min"))
        b = normalize_claim(path, dict(value=1, units="HOUR"))
        self.assertEqual(a, b)

    def test_feed_rate_alias_and_rpm(self):
        speed = normalize_claim("/conditions/mechanochemistry/frequency", dict(value=150, units="rpm"))
        feed = normalize_claim("/conditions/mechanochemistry/feed_rate", dict(value=1, units="cm3/min"))
        self.assertAlmostEqual(speed["value"], 2.5)
        self.assertEqual(speed["units"], "HERTZ")
        self.assertGreater(feed["value"], 0)

    def test_oneof_conflict_rejected(self):
        with self.assertRaises(ValueError):
            normalize_claim("/inputs/a/components/0/amount",
                            dict(mass=dict(value=1, units="GRAM"), moles=dict(value=1, units="MOLE")))

    def test_nonfinite_and_integer_range_rejected(self):
        for path, value in [("/conditions/ph", float("nan")),
                            ("/conditions/mechanochemistry/number_of_balls", 2**40)]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                normalize_claim(path, value)

    def test_reserved_provenance_rejected(self):
        with self.assertRaises(ValueError):
            normalize_claim("/provenance/record_created/person/email", "invented@example.invalid")

    def test_numeric_map_keys_and_pointer_escapes(self):
        value = {}
        set_pointer(value, "/inputs/123/components/0/is_limiting", False)
        set_pointer(value, "/inputs/a~1b~0c/components/0/is_limiting", True)
        self.assertIs(value["inputs"]["123"]["components"][0]["is_limiting"], False)
        self.assertIn("a/b~c", value["inputs"])

    def test_bbox_bounds(self):
        for bbox in [[0, 0, 101, 20], [5, 5, 5, 20], [0, 0, float("inf"), 20]]:
            with self.subTest(bbox=bbox), self.assertRaises(ValueError):
                check_bbox(bbox, dict(width=100, height=100))

    def test_missing_fields_are_not_fabricated(self):
        value = normalize_claim("/inputs/a/components/0", dict(identifiers=[dict(type="NAME", value="unknown amount")]))
        self.assertNotIn("amount", value)

    def test_catalog_pages_and_concrete_paths(self):
        first = catalog("/inputs/123/components/0", 5)
        self.assertTrue(first["truncated"])
        self.assertTrue(any("{key}" in f["path"] for f in first["fields"]))
        second = catalog("/inputs/123/components/0", 5, first["next_offset"])
        self.assertNotEqual(first["fields"], second["fields"])


@unittest.skipUnless(PAPERS, "Set CMCCDB_TEST_PAPERS to the supplied PDF corpus")
class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="harness-tests-", dir=TEST_ROOT)
        cls.pipeline = Pipeline([PAPERS], cls.tmp.name, POPPER)
        cls.main = cls.pipeline.create_run("d6mr00048g.pdf", curator_name="Fixture reviewer",
            curator_email="fixture@example.invalid", idempotency_key="setup")
        cls.sid = cls.main["sources"][0]["source_id"]
        index, _ = cls.pipeline.index(cls.main["run_id"], cls.sid)
        cls.line = next(l for l in index["lines"] if "and NaOH were milled at 30 Hz" in l["text"])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def fresh(self):
        # New state reuses the immutable source archive but gets distinct IDs.
        run = self.pipeline.create_run("d6mr00048g.pdf", curator_name="Fixture reviewer",
            curator_email="fixture@example.invalid")
        exp = dict(key="example", label="Method C illustrative extraction", evidence_ids=[self.line["evidence_id"]],
                   scope="Frequency-only integration fixture; other fields intentionally incomplete")
        task = dict(task_id="conditions", experiment_keys=["example"],
                    allowed_paths=["/conditions/mechanochemistry"], evidence_ids=[self.line["evidence_id"]],
                    instructions="Extract milling frequency from the cited line", required_workers=2)
        plan = dict(experiments=[exp], tasks=[task], source_search_complete=False,
                    coverage_notes=["Fixture extracts one reported condition, not the complete paper"])
        self.pipeline.set_plan(run["run_id"], plan)
        return run["run_id"], plan

    def claim(self, frequency=30, basis="reported"):
        return dict(experiment_key="example", path="/conditions/mechanochemistry/frequency",
                    value=dict(value=frequency, units="HERTZ"),
                    evidence=[dict(evidence_id=self.line["evidence_id"], quote="30 Hz")],
                    source_value="30 Hz", basis=basis,
                    explanation="Fixture interpretation" if basis == "inferred" else "")

    def submit_worker(self, run, worker, claims=None, abstentions=None):
        packet = self.pipeline.lease(run, worker, "recorded-fixture-v1", "fixture")
        self.assertTrue(packet["available"])
        return self.pipeline.submit(run, "conditions", worker, packet["lease_token"],
                                    dict(claims=claims or [], abstentions=abstentions or [])), packet

    def test_all_supplied_papers_index_and_ground_locations(self):
        papers = self.pipeline.list_papers()["papers"]
        self.assertGreaterEqual(len(papers), 8)
        for paper in papers:
            with self.subTest(paper=paper["filename"]):
                run = self.pipeline.create_run(paper["path"])
                source = run["sources"][0]
                self.assertGreater(source["pages"], 0)
                self.assertGreater(source["text_lines"], 0)
                index, _ = self.pipeline.index(run["run_id"], source["source_id"])
                line = index["lines"][0]
                self.assertEqual(self.pipeline.evidence(run["run_id"], [line["evidence_id"]])[0], line)
                self.assertEqual(len(source["sha256"]), 64)

    def test_control_glyphs_and_image_only_pages_are_explicit(self):
        xml = b'<html xmlns="http://www.w3.org/1999/xhtml"><page width="100" height="100"><line xMin="1" yMin="1" xMax="20" yMax="10"><word>a\x02b</word></line></page><page width="100" height="100"/></html>'
        with patch("cmccdb_extraction.documents.command", return_value=xml):
            index = index_pdf(Path(PAPERS)/"d6mr00048g.pdf", POPPER)
        self.assertEqual(index["text_substitutions"], {"U+0002": 1})
        self.assertEqual(index["lines"][0]["text"], "a\ufffdb")
        self.assertTrue(index["pages"][1]["needs_visual_reading"])

    def test_source_checksum_changes_are_rejected(self):
        run = self.pipeline.create_run("d6mr00048g.pdf")
        sid = run["sources"][0]["source_id"]
        _, path = self.pipeline.index(run["run_id"], sid)
        path.write_bytes(path.read_bytes() + b'\nfixture alteration')
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.pipeline.index(run["run_id"], sid)

    def test_changed_schema_blocks_resume(self):
        run, _ = self.fresh()
        with patch("cmccdb_extraction.pipeline.schema_hash", return_value="changed"):
            with self.assertRaisesRegex(ValueError, "schema changed"):
                self.pipeline.lease(run, "fixture", "fixture-v1", "fixture")

    def test_failed_roundtrip_removes_incomplete_export(self):
        from cmccdb_extraction.export import export_run
        run, _ = self.fresh()
        for worker in ["a", "b"]:
            self.submit_worker(run, worker, [self.claim()])
        assembled = self.pipeline.assemble(run)
        mutated = dataset_from_json(assembled["dataset"])
        mutated.reactions[0].conditions.mechanochemistry.frequency.value = 100
        def fake_backend(args, **kwargs):
            import subprocess
            Path(args[-1]).write_bytes(b"not a real workbook; mocked backend failure check")
            return subprocess.CompletedProcess(args, 0, "", "")
        self.pipeline.node = "configured-test-backend"
        with patch("cmccdb_extraction.export.subprocess.run", side_effect=fake_backend), \
             patch.object(DatasetConstructor, "enumerate_spreadsheet", return_value=mutated):
            with self.assertRaisesRegex(ValueError, "round-trip changed"):
                export_run(self.pipeline, run, assembled, True)
        self.assertEqual(list((self.pipeline.store.run_dir(run)/"exports").iterdir()), [])

    def test_source_root_escape_and_unknown_evidence_rejected(self):
        with self.assertRaises(ValueError):
            self.pipeline.create_run("../../completed/nonexistent.pdf")
        with self.assertRaises(ValueError):
            self.pipeline.evidence(self.main["run_id"], [self.sid + ":p9999:l0001"])

    def test_idempotency_matches_inputs(self):
        self.assertEqual(self.main, self.pipeline.create_run("d6mr00048g.pdf", curator_name="Fixture reviewer",
            curator_email="fixture@example.invalid", idempotency_key="setup"))
        with self.assertRaises(ValueError):
            self.pipeline.create_run("d6mr00025h.pdf", idempotency_key="setup")

    def test_si_role_is_preserved(self):
        si = Path(PAPERS) / "d6mr00048g1_suppl.pdf"
        if not si.is_file():
            self.skipTest("Supporting information not supplied")
        run = self.pipeline.create_run("d6mr00048g.pdf", [si.name])
        self.assertEqual([s["role"] for s in run["sources"]], ["paper", "supporting_information"])

    def test_duplicate_sources_rejected(self):
        with self.assertRaises(ValueError):
            self.pipeline.create_run("d6mr00048g.pdf", ["d6mr00048g.pdf"])

    def test_plan_cycles_and_unknown_paths_rejected(self):
        run, plan = self.fresh()
        self.assertTrue(self.pipeline.set_plan(run, plan)["unchanged"])
        other = self.pipeline.create_run("d6mr00048g.pdf")["run_id"]
        bad = copy.deepcopy(plan)
        bad["tasks"][0]["allowed_paths"] = ["/conditions/imaginary"]
        with self.assertRaises(ValueError):
            self.pipeline.set_plan(other, bad)
        bad = copy.deepcopy(plan)
        bad["tasks"][0]["depends_on"] = ["conditions"]
        with self.assertRaises(ValueError):
            self.pipeline.set_plan(other, bad)

    def test_concurrent_leases_are_bounded(self):
        run, _ = self.fresh()
        with ThreadPoolExecutor(max_workers=5) as pool:
            packets = list(pool.map(lambda i:self.pipeline.lease(run, f"worker-{i}", "fixture", "fixture"), range(5)))
        self.assertEqual(sum(p["available"] for p in packets), 2)

    def test_lease_expiration_and_restart(self):
        run, _ = self.fresh()
        packet = self.pipeline.lease(run, "worker", "fixture", "fixture")
        with self.pipeline.store.connect(write=True) as con:
            con.execute("UPDATE assignments SET expires=0 WHERE run=?", (run,))
        with self.assertRaises(ValueError):
            self.pipeline.submit(run, "conditions", "worker", packet["lease_token"], dict(claims=[self.claim()]))
        restarted = Pipeline([PAPERS], self.tmp.name, POPPER)
        new = restarted.lease(run, "worker", "fixture", "fixture")
        self.assertNotEqual(new["lease_token"], packet["lease_token"])
        self.assertEqual(restarted.status(run)["manifest"]["dataset_id"], self.pipeline.status(run)["manifest"]["dataset_id"])

    def test_quote_rejection_is_atomic(self):
        run, _ = self.fresh()
        packet = self.pipeline.lease(run, "worker", "fixture", "fixture")
        wrong = self.claim()
        wrong["evidence"][0]["quote"] = "This sentence is not in this paper"
        with self.assertRaises(ValueError):
            self.pipeline.submit(run, "conditions", "worker", packet["lease_token"], dict(claims=[self.claim(), wrong]))
        self.assertEqual(self.pipeline.status(run)["claim_count"], 0)

    def test_single_vote_and_abstention_do_not_create_consensus(self):
        run, _ = self.fresh()
        self.submit_worker(run, "a", [self.claim()])
        self.submit_worker(run, "b", abstentions=["Cannot establish applicable procedure"])
        self.assertEqual(self.pipeline.consensus(run)["fields"][0]["status"], "needs_review")

    def test_agreement_and_idempotent_submit(self):
        run, _ = self.fresh()
        result, packet = self.submit_worker(run, "a", [self.claim()])
        retry = self.pipeline.submit(run, "conditions", "a", packet["lease_token"], dict(claims=[self.claim()]))
        self.assertTrue(retry["unchanged"])
        self.submit_worker(run, "b", [self.claim()])
        self.assertEqual(self.pipeline.consensus(run)["fields"][0]["status"], "agreed")
        self.assertEqual(self.pipeline.status(run)["claim_count"], 2)

    def test_conflict_adjudication_is_logged_and_does_not_average(self):
        run, _ = self.fresh()
        self.submit_worker(run, "a", [self.claim(30)])
        self.submit_worker(run, "b", [self.claim(31)])
        field = self.pipeline.consensus(run)["fields"][0]
        self.assertEqual(field["status"], "needs_review")
        right = next(c for c in field["candidates"] if c["value"]["value"] == 30)
        self.pipeline.resolve(run, "example", field["path"], right["claim_id"], "Fixture reviewer", "The source says 30 Hz")
        resolved = self.pipeline.consensus(run)["fields"][0]
        self.assertEqual(resolved["value"]["value"], 30)
        self.assertEqual(resolved["status"], "reviewed")
        self.assertIn("claim_adjudicated", [e["kind"] for e in self.pipeline.store.audit(run)["events"]])

    def test_agreed_inference_still_requires_review(self):
        run, _ = self.fresh()
        self.submit_worker(run, "a", [self.claim(basis="inferred")])
        self.submit_worker(run, "b", [self.claim(basis="inferred")])
        self.assertEqual(self.pipeline.consensus(run)["fields"][0]["status"], "needs_review")

    def test_missing_required_data_blocks_final_export(self):
        run, _ = self.fresh()
        self.submit_worker(run, "a", [self.claim()])
        self.submit_worker(run, "b", [self.claim()])
        validation = self.pipeline.assemble(run)["validation"]
        self.assertFalse(validation["valid"])
        self.assertFalse(validation["ready_for_contribution"])
        with self.assertRaises(ValueError):
            self.pipeline.export(run)

    def test_view_pixels_and_visual_evidence(self):
        path, metadata = self.pipeline.page(self.main["run_id"], self.sid, 3, [30.0, 30.0, 300.0, 400.0], 80)
        self.assertTrue(path.read_bytes().startswith(b"\x89PNG"))
        from cmccdb_extraction.contracts import EvidenceRef
        reference = EvidenceRef(evidence_id=self.sid + ":p0003", bbox=[30.0,30.0,300.0,400.0],
                                observation="Fixture reads a table region; no scientific result asserted")
        self.pipeline.checked_reference(self.main["run_id"], reference)


if __name__ == "__main__":
    unittest.main()
