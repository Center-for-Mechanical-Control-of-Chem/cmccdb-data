"""Reproducible reference replay over real MCP, NOT an LLM extraction benchmark.

These manually selected values cover Compound 3 / Methods C.1–C.3 only.
The two fixture identities test protocol/consensus plumbing; they are not
independent scientific opinions. Run with --export to produce a draft XLSX.
"""

import argparse
import asyncio
import hashlib
import json
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

HASHES = ["8c64e992f2a92fb501ef35343bd02b87031f911a6e1aa94b21fe6d97b60f5197",
          "2dedd0f3b26c05330eaeca081eda6e60108af164ed87faacd9f8db3e08da7b84"]


async def call(session, name, **arguments):
    result = await session.call_tool(name, arguments)
    if result.is_error:
        raise ValueError("; ".join(c.text for c in result.content if hasattr(c, "text")))
    return result.structured_content or json.loads(next(c.text for c in result.content if hasattr(c, "text")))


async def replay(session, export=False):
    manifest = await call(session, "create_run", paper="d6mr00048g.pdf",
        supporting_information=["d6mr00048g1_suppl.pdf"],
        dataset_name="REFERENCE FIXTURE — Compound 3 / C.1, C.2, C.3",
        curator_name="Reference fixture curator", curator_email="fixture@example.invalid")
    if [s["sha256"] for s in manifest["sources"]] != HASHES:
        raise ValueError("Reference PDFs changed; review citations and values before updating the fixture")
    run = manifest["run_id"]
    main, si = [s["source_id"] for s in manifest["sources"]]
    lines = []
    for sid, page in [(main, 1), (main, 7), (si, 3)]:
        packet = await call(session, "read_source_page", run_id=run, source_id=sid, page=page, limit=300)
        lines += packet["lines"]

    def ref(page, fragment, sid=main, line=None):
        matches = [l for l in lines if l["source_id"] == sid and l["page"] == page and fragment in l["text"]
                   and (line is None or l["evidence_id"].endswith(f":l{line:04d}"))]
        if len(matches) != 1:
            raise ValueError(f"Reference citation must be unique: {fragment!r}")
        return dict(evidence_id=matches[0]["evidence_id"], quote=fragment)

    procedure = [ref(7, "Methods C.1, C.2, and C.3."), ref(7, "chemical reactions were performed using a mixer mill (Retsch"),
        ref(7, "MM400) equipped with 25 mL"), ref(7, "jars and 15 mm round PTFE milling balls."),
        ref(7, "a single ball per experiment."), ref(7, "the mixture was shaken at 30 Hz for 30–60 minutes at room"),
        ref(7, "temperature. Following the reaction")]
    method_lines = {
        "C1": [ref(7, "yield: no reaction; Method C.1: ball"), ref(7, "milling (60 min, 30 Hz)"),
               ref(7, "equiv.), isobutyl chloroformate (105 mg, 0.77 mmol, 1.2 equiv.),", line=99),
               ref(7, "yield: 138 mg; 84%")],
        "C2": [ref(7, "Method C.2: ball milling (30 min, 30 Hz)"),
               ref(7, "0.64 mmol, 1 equiv.), isobutyl chloroformate (105 mg,"),
               ref(7, "0.77 mmol, 1.2 equiv.), NaOH (33 mg, 0.83 mmol, 1.3 equiv.),"),
               ref(7, "Al 2 O 3 (neutral, 3.0 g); yield: 36 mg; 22%")],
        "C3": [ref(7, "yield: 36 mg; 22%; Method C.3: ball"), ref(7, "milling (30 min, 30 Hz), dec-9-en-1-ol (100 mg, 0.64 mmol, 1"),
               ref(7, "NaOH (33 mg, 0.83 mmol, 1.3 equiv.), NaCl (1.5 g); yield:"),
               ref(7, "128 mg; 78%")]
    }
    # Some lines wrap names and amounts. Always keep the whole method context.
    method_lines["C3"].append(next(dict(evidence_id=l["evidence_id"], quote=l["text"])
        for l in lines if l["evidence_id"] == main + ":p0007:l0106"))
    si_caption = ref(3, "Figure S1. 1 H NMR spectrum of 3", si)
    visual = dict(evidence_id=main + ":p0003", bbox=[40.0, 45.0, 295.0, 337.0],
                  observation="Table 1: entries 3, 7 and 10 report 84%, 22% and 78% isolated yield; inspect row labels and footnotes.")
    si_visual = dict(evidence_id=si + ":p0003", bbox=[35.0, 50.0, 565.0, 450.0],
                     observation="Figure S1: reported 1H NMR spectrum of Compound 3. This fixture does not assign peaks or infer a structure.")
    for sid, page, bbox in [(main, 3, visual["bbox"]), (si, 3, si_visual["bbox"])]:
        pixels = await session.call_tool("view_page", dict(run_id=run, source_id=sid, page=page, bbox=bbox, dpi=80))
        assert not pixels.is_error and any(c.type == "image" for c in pixels.content)
    experiments = [dict(key=key, label="Compound 3 / Method " + key.replace("C", "C."),
        evidence_ids=[r["evidence_id"] for r in refs],
        scope="One preparative result. Other products, optimization rows and non-mechanochemical controls are outside this fixture.")
        for key, refs in method_lines.items()]
    all_refs = procedure + [r for refs in method_lines.values() for r in refs] + [si_caption, visual, si_visual]
    evidence_ids = list(dict.fromkeys(r["evidence_id"] for r in all_refs))
    tasks = [dict(task_id=name, experiment_keys=[e["key"] for e in experiments], allowed_paths=paths,
        evidence_ids=evidence_ids, modality="mixed", required_workers=2, depends_on=dependencies,
        instructions=instructions) for name, paths, dependencies, instructions in [
            ("conditions", ["/conditions/mechanochemistry", "/conditions/temperature/control", "/provenance/doi"], [],
             "Link each method to the general procedure. Distinguish oscillation frequency from NMR frequency."),
            ("inputs", ["/inputs"], [], "Read complete wrapped reagent lists for each method. Treat role/limiting assignments as inference."),
            ("outcomes", ["/outcomes"], ["conditions", "inputs"], "Read method-specific yield and product mass; cross-check Table 1 and SI captions.")]]
    await call(session, "install_search_plan", run_id=run, plan=dict(experiments=experiments, tasks=tasks,
        source_search_complete=False, coverage_notes=[
            "REFERENCE REPLAY: two fixture identities reuse manually selected claims; no independent LLM benchmark is claimed.",
            "Only Compound 3 / C.1–C.3 is covered. All other experiments require a new, complete paper search plan.",
            "Reagent masses are reported; roles and limiting reagent assignments are explicitly adjudicated interpretations.",
            "The reported 15 mm ball size is not interpreted as a radius. Product structures and NMR peak assignments remain unextracted.",
            "Workup details, authorship attribution and complete analytical results remain to be extracted."] ))

    submissions = {task["task_id"]: [] for task in tasks}
    for key, duration, mass, yield_value in [("C1", 60, 138, 84), ("C2", 30, 36, 22), ("C3", 30, 128, 78)]:
        refs = method_lines[key]
        def claim(path, value, evidence, source_value, basis="reported", explanation=""):
            return dict(experiment_key=key, path=path, value=value, evidence=evidence,
                        source_value=source_value, basis=basis, explanation=explanation)
        submissions["conditions"] += [
            claim("/conditions/mechanochemistry", dict(type="BALL_MILL", frequency=dict(value=30, units="HERTZ"),
                duration=dict(value=duration, units="MINUTE"), ball_material="PTFE", cell_material="PTFE",
                number_of_balls=1, model_name="Retsch MM400"), procedure + refs, f"{duration} min; 30 Hz; Retsch MM400; single PTFE ball"),
            claim("/conditions/temperature/control", dict(type="AMBIENT"), procedure, "room temperature"),
            claim("/provenance/doi", "10.1039/d6mr00048g", [ref(1, "10.1039/d6mr00048g")], "10.1039/d6mr00048g")]
        reagents = [("alcohol", "dec-9-en-1-ol", 100, "REACTANT", True),
                    ("chloroformate", "isobutyl chloroformate", 105, "REACTANT", False),
                    ("base", "NaOH", 33, "REAGENT", False)]
        if key == "C2":
            reagents.append(("additive", "neutral Al2O3", 3000, "ADDITIVE", False))
        if key == "C3":
            reagents.append(("additive", "NaCl", 1500, "ADDITIVE", False))
        for input_key, name, mg, role, limiting in reagents:
            submissions["inputs"].append(claim(f"/inputs/{input_key}/components/0",
                dict(identifiers=[dict(type="NAME", value=name)], amount=dict(mass=dict(value=mg, units="MILLIGRAM")),
                     reaction_role=role, is_limiting=limiting), refs,
                f"{name}; {mg} mg", "inferred", "Mass is reported. Reactant/reagent/additive role and limiting designation are interpreted from the method and stated equivalents."))
        outcome = dict(products=[dict(identifiers=[dict(type="NAME", value="Compound 3")],
            reaction_role="PRODUCT", measurements=[dict(type="YIELD", percentage=dict(value=yield_value),
            analysis_key="isolated_mass"), dict(type="AMOUNT", amount=dict(mass=dict(value=mass, units="MILLIGRAM")),
            analysis_key="isolated_mass")])], analyses=dict(isolated_mass=dict(type="WEIGHT", is_of_isolated_species=True)))
        submissions["outcomes"].append(claim("/outcomes/0", outcome,
            refs + [ref(7, "Reported yields refer to pure"), ref(7, "compounds that are dried under high vacuum."),
                    si_caption, visual, si_visual], f"Compound 3; {mass} mg; {yield_value}% isolated yield", "inferred",
            "The product mass and yield are reported. WEIGHT/isolated-species analysis is inferred from the stated pure, dried products; SI supports the compound label, not method-specific spectroscopy."))
    # Independent identities here are deliberate test fixtures, with disclosure in
    # worker_kind, model_revision, dataset name and coverage notes.
    for task in tasks:
        for worker in ["reference-fixture-a", "reference-fixture-b"]:
            packet = await call(session, "lease_extraction_task", run_id=run, task_id=task["task_id"],
                worker_id=worker, model_revision="manual-carbonates-reference-v1", worker_kind="fixture")
            assert packet["available"]
            await call(session, "submit_extraction", run_id=run, task_id=task["task_id"], worker_id=worker,
                       lease_token=packet["lease_token"], submission=dict(claims=submissions[task["task_id"]]))
    consensus = await call(session, "inspect_consensus", run_id=run)
    for field in consensus["fields"]:
        if field["status"] == "needs_review":
            await call(session, "adjudicate_claim", run_id=run, experiment_key=field["experiment_key"],
                path=field["path"], claim_id=field["candidates"][0]["claim_id"], actor="manual-reference-review-v1",
                reason="Recorded fixture review of the cited method, general procedure and table. Interpretation is documented in the claim; this does not establish LLM agreement.")
    assembled = await call(session, "validate_extraction", run_id=run)
    assert len(assembled["dataset"]["reactions"]) == 3
    assert not assembled["validation"]["ready_for_contribution"]
    result = dict(manifest=manifest, validation=assembled["validation"], protocol="MCP stdio", mode="manual_reference_replay")
    if export:
        result["export"] = await call(session, "export_workbook", run_id=run, allow_partial=True)
        resource = await session.read_resource(result["export"]["resources"]["dataset.json"])
        assert resource.contents
    return result


async def main(args):
    command = ["-m", "cmccdb_extraction.server", "--paper-root", args.paper_root, "--work-root", args.work_root]
    if args.poppler_bin:
        command += ["--poppler-bin", args.poppler_bin]
    if args.node:
        command += ["--node", args.node]
    params = StdioServerParameters(command=sys.executable, args=command, env=dict(os.environ))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await replay(session, args.export)
    print(json.dumps(result, indent=2))
    if args.receipt:
        Path(args.receipt).write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper-root", required=True)
    parser.add_argument("--work-root", required=True)
    parser.add_argument("--poppler-bin")
    parser.add_argument("--node")
    parser.add_argument("--receipt")
    parser.add_argument("--export", action="store_true")
    asyncio.run(main(parser.parse_args()))
