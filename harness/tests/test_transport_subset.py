"""Whole-record transport regressions; fixtures are not scientific extractions."""

import copy
import unittest

from cmccdb_extraction.export import transport_subset


def assembled_fixture():
    one = dict(reaction_id="one", inputs={"charge": {"components": [
        {"amount": {"mass": {"value": 0, "units": "GRAM"}}, "is_limiting": False}]}},
        outcomes=[{"products": [{"measurements": [{"type": "YIELD", "percentage": {"value": 80}}]}]}])
    two = dict(reaction_id="two", notes={"procedure_details": "Second independent charge"})
    reports = [dict(experiment_key="exp-one", reaction_id="one", compounds=[{}, {}],
                    dataset_sha256="full-paper-hash", reaction_sha256="one-hash", claims_sha256="one-claims",
                    review_current=True, ready=True, errors=[]),
               dict(experiment_key="exp-two", reaction_id="two", compounds=[{}],
                    dataset_sha256="full-paper-hash", reaction_sha256="two-hash", claims_sha256="two-claims",
                    review_current=False, ready=False, errors=["Missing chemical identity review"])]
    changes = [dict(reaction_id="one", path="/notes/details", original=" first ", exported="first"),
               dict(reaction_id="two", path="/notes/details", original=" second ", exported="second")]
    return dict(dataset=dict(reactions=[one, two]),
        consensus=dict(fields=[dict(experiment_key="exp-one", path="/outcomes/0/products/0/measurements/0/percentage/value",
                                    value=80, candidates=[{"source_value": "80%", "evidence": ["original-reference"]}]),
                               dict(experiment_key="exp-two", path="/notes/procedure_details", value="Second independent charge")],
                       incomplete_tasks=["run-level-task"]),
        review=[dict(experiment_key="exp-one", status="withheld", decision={"claim_id": None}),
                dict(experiment_key="exp-one", status="unreported", decision={"claim_id": "unreported-claim"}),
                dict(experiment_key="exp-two", status="needs_review", decision=None)],
        chemistry_report=dict(dataset_sha256="full-paper-hash", reactions=reports, ready=False,
            counts=dict(reactions=2, compounds=3, reviewed_reactions=1, ready_reactions=1)),
        validation=dict(valid=True, ready_for_contribution=False, chemistry_ready=False,
                        chemistry_blockers=[dict(experiment_key="exp-two", errors=["Missing chemical identity review"])],
                        unresolved_fields=1, incomplete_tasks=["run-level-task"], source_search_complete=False,
                        coverage_notes=["Original unresolved coverage"], text_boundary_normalizations=copy.deepcopy(changes)),
        text_boundary_normalizations=changes)


class TransportSubsetTests(unittest.TestCase):
    def test_part_excludes_other_reactions_chemistry_and_blockers(self):
        source = assembled_fixture()
        part = transport_subset(source, [source["dataset"]["reactions"][0]], {"exp-one"}, "Part one")
        self.assertEqual([r["reaction_id"] for r in part["dataset"]["reactions"]], ["one"])
        self.assertEqual([r["experiment_key"] for r in part["chemistry_report"]["reactions"]], ["exp-one"])
        self.assertEqual({f["experiment_key"] for f in part["consensus"]["fields"]}, {"exp-one"})
        self.assertEqual({f["experiment_key"] for f in part["review"]}, {"exp-one"})
        self.assertEqual(part["validation"]["chemistry_blockers"], [])
        self.assertEqual(part["chemistry_report"]["counts"],
                         dict(reactions=1, compounds=2, reviewed_reactions=1, ready_reactions=1))
        self.assertEqual(part["validation"]["unresolved_fields"], 0)
        self.assertTrue(part["validation"]["chemistry_ready"])
        for container in (part, part["validation"]):
            self.assertEqual([c["reaction_id"] for c in container["text_boundary_normalizations"]], ["one"])

    def test_partition_does_not_promote_run_level_draft(self):
        source = assembled_fixture()
        part = transport_subset(source, source["dataset"]["reactions"][:1], {"exp-one"}, "Part one")
        self.assertTrue(part["chemistry_report"]["ready"])
        self.assertFalse(part["validation"]["ready_for_contribution"])
        self.assertFalse(part["validation"]["source_search_complete"])
        self.assertEqual(part["validation"]["incomplete_tasks"], ["run-level-task"])
        self.assertEqual(part["validation"]["coverage_notes"], ["Original unresolved coverage"])

    def test_blocked_part_keeps_its_blocker_and_exact_counts(self):
        source = assembled_fixture()
        part = transport_subset(source, source["dataset"]["reactions"][1:], {"exp-two"}, "Part two")
        self.assertEqual(part["chemistry_report"]["counts"],
                         dict(reactions=1, compounds=1, reviewed_reactions=0, ready_reactions=0))
        self.assertFalse(part["chemistry_report"]["ready"])
        self.assertEqual(part["validation"]["unresolved_fields"], 1)
        self.assertEqual(part["validation"]["chemistry_blockers"], source["validation"]["chemistry_blockers"])

    def test_ready_full_run_keeps_ready_whole_record_parts(self):
        source = assembled_fixture()
        source["review"] = []
        source["consensus"]["incomplete_tasks"] = []
        source["validation"].update(ready_for_contribution=True, chemistry_ready=True,
            chemistry_blockers=[], unresolved_fields=0, incomplete_tasks=[], source_search_complete=True)
        for reaction in source["chemistry_report"]["reactions"]:
            reaction.update(review_current=True, ready=True, errors=[])
        part = transport_subset(source, source["dataset"]["reactions"][:1], {"exp-one"}, "Ready part")
        self.assertTrue(part["validation"]["ready_for_contribution"])
        self.assertTrue(part["chemistry_report"]["ready"])
        self.assertEqual(part["chemistry_report"]["counts"],
                         dict(reactions=1, compounds=2, reviewed_reactions=1, ready_reactions=1))

    def test_complementary_parts_preserve_whole_scientific_records_and_review_bindings(self):
        source = assembled_fixture()
        before = copy.deepcopy(source)
        parts = [transport_subset(source, [source["dataset"]["reactions"][i]], {key}, key)
                 for i, key in enumerate(("exp-one", "exp-two"))]
        self.assertEqual([r for p in parts for r in p["dataset"]["reactions"]], source["dataset"]["reactions"])
        self.assertEqual(source, before)
        for i, part in enumerate(parts):
            self.assertEqual(part["chemistry_report"]["dataset_sha256"], "full-paper-hash")
            self.assertEqual(part["chemistry_report"]["reactions"][0], source["chemistry_report"]["reactions"][i])
        self.assertEqual(parts[0]["consensus"]["fields"][0], source["consensus"]["fields"][0])
        parts[0]["dataset"]["reactions"][0]["inputs"]["charge"]["components"][0]["amount"]["mass"]["value"] = 999
        parts[0]["chemistry_report"]["reactions"][0]["compounds"].append({"changed": True})
        self.assertEqual(source, before)

    def test_rejects_mismatched_record_and_chemistry_selection(self):
        source = assembled_fixture()
        with self.assertRaisesRegex(ValueError, "chemistry does not match"):
            transport_subset(source, source["dataset"]["reactions"][:1], {"exp-two"}, "Wrong mapping")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            transport_subset(source, [source["dataset"]["reactions"][0]] * 2, {"exp-one"}, "Duplicate")
        with self.assertRaisesRegex(ValueError, "outside"):
            transport_subset(source, [{"reaction_id": "not-present"}], {"exp-one"}, "Unknown record")

    def test_legacy_assembly_without_chemistry_report_remains_supported(self):
        source = assembled_fixture()
        del source["chemistry_report"]
        part = transport_subset(source, source["dataset"]["reactions"][:1], {"exp-one"}, "Legacy part")
        self.assertNotIn("chemistry_report", part)
        self.assertEqual(len(part["dataset"]["reactions"]), 1)
        self.assertFalse(part["validation"]["ready_for_contribution"])


if __name__ == "__main__":
    unittest.main()
