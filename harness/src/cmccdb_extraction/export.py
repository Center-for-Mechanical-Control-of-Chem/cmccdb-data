"""Inverse CMCCDB spreadsheet layout with mandatory protobuf round-trip checks.

The Python layer describes cells. A configured Node/Artifact Tool backend writes
the XLSX. This backend is replaceable without changing evidence or schema logic.
"""

import copy
import base64
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import struct
import math
from collections import OrderedDict
from pathlib import Path

from google.protobuf import json_format
from cmccdb_schema import dataset_constructor
from cmccdb_schema.proto import reaction_pb2
from .schema import canonical, escaped, is_map, repeated, dataset_from_json
from .audit_transport import prepare_audit_transport, audit_documentation_rows
from .auxiliary_partition import MAX_ATTACHMENT_BYTES

# Leave 1 MiB below the interface's 31 MiB request limit for multipart metadata.
MAX_REQUEST_BYTES = 30 * 1024 * 1024
MAX_AUXILIARY_FILES = 64


def flatten(desc, data, pointer="", labels=(), groups=()):
    columns = []
    for field in desc.fields:
        if field.name not in data:
            continue
        value = data[field.name]
        path = pointer + "/" + escaped(field.name)
        names = labels + (field.name,)
        if is_map(field):
            vf = field.message_type.fields_by_name["value"]
            for key, entry in value.items():
                child = path + "/" + escaped(key)
                branch = groups + (child,)
                columns.append(dict(path=child + "/@key", labels=names + ("key",),
                                    groups=branch + (child + "/@key",), value=key))
                if vf.message_type:
                    columns += flatten(vf.message_type, entry, child, names, branch)
                else:
                    columns.append(dict(path=child, labels=names + ("value",),
                                        groups=branch + (child + "/value",), value=entry))
        elif repeated(field):
            for index, item in enumerate(value):
                child = path + "/" + str(index)
                branch = groups + (child,)
                if field.message_type:
                    columns += flatten(field.message_type, item, child, names, branch)
                else:
                    columns.append(dict(path=child, labels=names, groups=branch, value=item))
        elif field.message_type:
            columns += flatten(field.message_type, value, path, names, groups + (path,))
        else:
            columns.append(dict(path=path, labels=names, groups=groups + (path,), value=value))
    return columns


def header_rows(columns):
    depth = max(len(c["labels"]) for c in columns)
    rows = [[""] * len(columns) for _ in range(depth)]
    for index, column in enumerate(columns):
        for level, label in enumerate(column["labels"]):
            if index == 0 or column["groups"][:level + 1] != columns[index - 1]["groups"][:level + 1]:
                rows[level][index] = label
    return rows


def common_provenance(reactions):
    first = reactions[0].get("provenance", {})
    keys = [k for k in first if all(r.get("provenance", {}).get(k) == first[k] for r in reactions)]
    common = dict(provenance={k: copy.deepcopy(first[k]) for k in keys})
    if not common["provenance"]:
        raise ValueError("Export requires shared record creation provenance")
    return common


def spreadsheet_rows(dataset):
    reactions = dataset["reactions"]
    if not reactions:
        raise ValueError("Cannot export an empty extraction as reaction data")
    common = common_provenance(reactions)
    common_columns = flatten(reaction_pb2.Reaction.DESCRIPTOR, common)
    common_headers = header_rows(common_columns)
    blocks = OrderedDict()
    for reaction in reactions:
        variant = copy.deepcopy(reaction)
        for key in common["provenance"]:
            variant["provenance"].pop(key)
        if not variant["provenance"]:
            variant.pop("provenance")
        columns = flatten(reaction_pb2.Reaction.DESCRIPTOR, variant)
        headers = header_rows(columns)
        key = canonical(headers)
        blocks.setdefault(key, dict(headers=headers, records=[]))["records"].append((reaction, columns))
    rows, data_cells, header_indices = [], [], []
    for block in blocks.values():
        rows.append(["REACTION"])
        header_indices.append(len(rows))
        header_indices += list(range(len(rows)+1, len(rows)+1+len(common_headers)))
        rows += [[""] + header for header in common_headers]
        rows.append([""] + [c["value"] for c in common_columns])
        rows.append(["VARIANTS"])
        header_indices.append(len(rows))
        header_indices += list(range(len(rows)+1, len(rows)+1+len(block["headers"])))
        rows += [[""] + header for header in block["headers"]]
        rows.append(["DATA"])
        header_indices.append(len(rows))
        for reaction, columns in block["records"]:
            rows.append([""] + [c["value"] for c in columns])
            for i, column in enumerate(columns, 2):
                data_cells.append(dict(reaction_id=reaction["reaction_id"], path=column["path"],
                                       row=len(rows), column=i, value=column["value"]))
        rows.append(["#! End of reaction block"])
        # Blank separates blocks for compatibility with older converter releases.
        rows.append([])
    width = max(len(r) for r in rows)
    if width > 16384 or len(rows) > 1048576:
        raise ValueError("Extraction exceeds Excel worksheet limits")
    rows = [r + [""] * (width - len(r)) for r in rows]
    return rows, data_cells, header_indices


def address(row, column):
    text = ""
    while column:
        column, remain = divmod(column - 1, 26)
        text = chr(65 + remain) + text
    return text + str(row)


def table_sheet(name, headers, rows, widths=None):
    # All non-data rows are marked as comments for DatasetConstructor.
    return dict(name=name, rows=[["#!"] + headers] + [["#!"] + r for r in rows],
                widths=[4] + (widths or [24] * len(headers)), header_rows=[1], freeze_rows=1)


def export_text_boundaries(dataset):
    """Declare the existing converter's boundary-whitespace normalization.

    Original worker values remain in the immutable audit. No internal text,
    numbers, units or array positions are changed by this export accommodation.
    """
    normalized = copy.deepcopy(dataset)
    changes = []
    for reaction in normalized["reactions"]:
        for column in flatten(reaction_pb2.Reaction.DESCRIPTOR, reaction):
            value = column["value"]
            if isinstance(value, str) and value != value.strip():
                if column["path"].endswith("/@key"):
                    raise ValueError("Whitespace in map keys requires explicit manager remapping")
                changes.append(dict(reaction_id=reaction["reaction_id"], path=column["path"],
                                    original=value, exported=value.strip()))
                tokens = [s.replace("~1", "/").replace("~0", "~") for s in column["path"].split("/")[1:]]
                parent = reaction
                for token in tokens[:-1]:
                    parent = parent[int(token)] if isinstance(parent, list) else parent[token]
                if isinstance(parent, list):
                    parent[int(tokens[-1])] = value.strip()
                else:
                    parent[tokens[-1]] = value.strip()
    return normalized, changes


def workbook_plan(pipeline, run, assembled, allow_partial=False):
    rows, cells, headers = spreadsheet_rows(assembled["dataset"])
    audit = pipeline.store.audit(run)
    experiments = {e["reaction_id"]: e["key"] for e in audit["experiments"]}
    fields = {(f["experiment_key"], f["path"]): f for f in assembled["consensus"]["fields"]}
    by_experiment = {}
    for (key, path), field in fields.items():
        by_experiment.setdefault(key, []).append((path, field))
    notes = []
    for cell in cells:
        key = experiments[cell["reaction_id"]]
        claims = []
        for path, field in by_experiment.get(key, []):
            if cell["path"] == path or cell["path"].startswith(path + "/"):
                claims.extend(field["candidates"])
        if claims:
            text = "\n".join(f"{c['claim_id']}: " + "; ".join(e["evidence_id"] for e in c["evidence"])
                             for c in claims)
            notes.append(dict(cell=address(cell["row"], cell["column"]), text=text))
    data_widths = [14] + [20] * (max(map(len,rows)) - 1)
    for cell in cells:
        value = rows[cell['row']-1][cell['column']-1]
        if isinstance(value, str) and len(value)>60:
            data_widths[cell['column']-1] = max(data_widths[cell['column']-1], min(72,max(32,len(value)//4)))
    data = dict(name="ReactionData", rows=rows, notes=notes, header_rows=headers,
                widths=data_widths, freeze_rows=0)
    validation = assembled["validation"]
    review_rows = [["Run", run, "Development review draft: requires human scientific review" if allow_partial else
                    "Ready for contribution" if validation["ready_for_contribution"] else
                    "Draft: extraction or review is incomplete"]]
    if assembled.get("transport_partition"):
        review_rows.append(["Transport partition", "", assembled["transport_partition"]])
    for error in validation["errors"]:
        review_rows.append(["Validation error", "", error])
    for warning in validation["warnings"]:
        review_rows.append(["Validation warning", "", warning])
    for field in assembled["review"]:
        review_rows.append([field["experiment_key"], field["path"], field["status"]])
    for task in validation["incomplete_tasks"]:
        review_rows.append(["Incomplete task", task, "Needs independent worker completion"])
    for note in validation["coverage_notes"]:
        review_rows.append(["Coverage", "", note])
    review = table_sheet("Review", ["Experiment or check", "Field or task", "Finding"], review_rows, [28, 48, 85])
    evidence_rows = []
    figures = OrderedDict()
    for field in assembled["consensus"]["fields"]:
        for c in field["candidates"]:
            evidence_rows.append([c["experiment_key"], c["path"], c["source_value"], canonical(c["normalized_value"]),
                c["basis"], field["status"], "; ".join(e["evidence_id"] for e in c["evidence"]),
                c["worker_id"], c["model_revision"], c["claim_id"],
                "\n".join(e["quote"] or e["observation"] for e in c["evidence"]), c["explanation"]])
            for ref in c["evidence"]:
                if ref["bbox"]:
                    figures.setdefault(canonical([ref["evidence_id"], ref["bbox"]]), ref)
    evidence = table_sheet("Evidence", ["Experiment", "Schema field", "Source value", "Normalized value", "Basis",
        "Decision", "Source locations", "Worker", "Model revision", "Claim ID", "Quote or visual observation",
        "Interpretation"], evidence_rows, [24, 46, 36, 42, 18, 20, 60, 24, 30, 44, 85, 85])
    sources = table_sheet("Sources", ["File", "Role", "DOI hint", "Pages", "Source ID", "SHA256"],
        [[s["filename"], s["role"], s["doi_hint"] or "", s["pages"], s["source_id"], s["sha256"]]
         for s in audit["manifest"]["sources"]], [30, 28, 30, 12, 40, 72])
    schema = table_sheet("Schema", ["Property", "Value"],
        [["Schema SHA256", audit["manifest"]["schema_sha256"]], ["Harness version", "0.1.0"],
         ["Percentages", "50% is stored as 50, not 0.5"],
         ["Audit attachment", "extraction-audit.json.gz (lossless compressed JSON)"],
         ["Edits", "An edited value must be revalidated and its evidence/review decision reconsidered"]], [28, 90])
    labels = {e["key"]: e["data"]["label"] for e in audit["experiments"]}
    overview_rows = []
    def quantity(value):
        return f"{value.get('value', '')} {value.get('units', '')}".strip()
    for reaction in assembled["dataset"]["reactions"]:
        key = experiments[reaction["reaction_id"]]
        mech = reaction.get("conditions", {}).get("mechanochemistry", {})
        components = [c for inp in reaction.get("inputs", {}).values() for c in inp.get("components", [])]
        inputs = "; ".join(next((i["value"] for i in c.get("identifiers", []) if i.get("type") == "NAME"), "Unidentified")
            for c in components)
        products = [p for outcome in reaction.get("outcomes", []) for p in outcome.get("products", [])]
        yields = [str(m["percentage"]["value"]) + "%" for p in products for m in p.get("measurements", [])
                  if m.get("type") == "YIELD" and "percentage" in m]
        overview_rows.append([key, labels[key], inputs, mech.get("type", ""), quantity(mech.get("frequency", {})),
                              quantity(mech.get("duration", {})), "; ".join(yields), reaction["reaction_id"]])
    overview = table_sheet("Overview", ["Experiment", "Record", "Inputs", "Reactor", "Frequency", "Duration", "Yield", "Reaction ID"],
        overview_rows, [20, 40, 80, 24, 22, 22, 20, 48])
    overview["rows"].insert(1, ["#!", "Read-only overview", "Edit ReactionData and revalidate. See Review for incomplete coverage."])
    sheets = [overview, data, review, evidence, sources, schema]
    if assembled.get("chemistry_report"):
        chemistry_rows = []
        for reaction in assembled["chemistry_report"]["reactions"]:
            for compound in reaction["compounds"]:
                structures = compound["structures"]
                crystal = compound["crystal_parameters"]
                chemistry_rows.append([reaction["experiment_key"], compound["compound_path"],
                    "; ".join(compound["names"]), compound["status"],
                    "\n".join(s.get("canonical_isomeric_smiles", s.get("source_value", "")) for s in structures),
                    "\n".join(s.get("formula", "") for s in structures),
                    compound.get('structure_scope',''),compound.get('representation_limit',''),
                    canonical(crystal) if crystal else "",
                    "Current" if reaction["review_current"] else "Missing or stale",
                    reaction.get("review_actor") or "", "; ".join(compound["errors"]),
                    "; ".join(reaction["errors"])])
        sheets.append(table_sheet("Chemistry", ["Experiment", "Compound path", "Source names", "Structure status",
            "SMILES", "Graph formula", "Representation", "Representation limits", "CrystalParameters", "Manager review", "Reviewer", "Compound blockers", "Reaction blockers"],
            chemistry_rows, [24, 50, 44, 24, 85, 24, 32, 90, 70, 24, 35, 90, 90]))
        for blocker in assembled["validation"].get("chemistry_blockers", []):
            review["rows"].append(["#!", blocker["experiment_key"], "Chemistry review", "; ".join(blocker["errors"])])
    if figures:
        if len(figures) > 200:
            raise ValueError("More than 200 distinct visual citations; split into smaller reviewed runs")
        figure_rows, images = [["#!", "Cited source crops", "Original PDF coordinates; observations require review"]], []
        for ref in figures.values():
            page = pipeline.evidence(run, [ref["evidence_id"]])[0]
            image, _ = pipeline.page(run, page["source_id"], page["page"], ref["bbox"], dpi=110)
            raw = image.read_bytes()
            width, height = struct.unpack(">II", raw[16:24])  # PNG IHDR dimensions
            scale = min(1.0, 660 / width, 600 / height)
            figure_rows.append(["#!", ref["evidence_id"], ref["observation"]])
            figure_rows.append(["#!", "Bounding box", canonical(ref["bbox"])])
            start = len(figure_rows)
            images.append(dict(dataUrl="data:image/png;base64," + base64.b64encode(raw).decode(),
                anchor=dict(from_=dict(row=start, col=1), extent=dict(widthPx=width * scale, heightPx=height * scale))))
            images[-1]["anchor"]["from"] = images[-1]["anchor"].pop("from_")
            figure_rows += [["#!", "", ""] for _ in range(math.ceil(height * scale / 29.3) + 2)]
        sheets.append(dict(name="Figures", rows=figure_rows, images=images, widths=[4, 45, 100],
                           header_rows=[1], freeze_rows=1))
    documentation = audit_documentation_rows(assembled.get('audit_transport', {}))
    for sheet in sheets:
        if sheet['name'] in documentation:
            sheet['rows'].append(documentation[sheet['name']])
    return dict(sheets=sheets, data_cells=cells)


def transport_subset(assembled, reactions, experiment_keys, note):
    """Select whole records for transport; never discard fields or audit claims."""
    subset = copy.deepcopy(assembled)
    experiment_keys = set(experiment_keys)
    reaction_ids = [r["reaction_id"] for r in reactions]
    if len(reaction_ids) != len(set(reaction_ids)):
        raise ValueError("Transport subset contains duplicate reaction IDs")
    if not set(reaction_ids) <= {r["reaction_id"] for r in assembled["dataset"]["reactions"]}:
        raise ValueError("Transport subset contains a reaction outside the original dataset")
    subset["dataset"]["reactions"] = copy.deepcopy(reactions)
    subset["consensus"]["fields"] = [f for f in subset["consensus"]["fields"]
                                       if f["experiment_key"] in experiment_keys]
    subset["review"] = [f for f in subset["review"] if f["experiment_key"] in experiment_keys]
    chemistry = subset.get("chemistry_report")
    if chemistry is not None:
        chemistry["reactions"] = [r for r in chemistry["reactions"]
                                   if r["experiment_key"] in experiment_keys]
        selected = chemistry["reactions"]
        if (len(selected) != len(reaction_ids) or
            {r["experiment_key"] for r in selected} != experiment_keys or
            {r["reaction_id"] for r in selected} != set(reaction_ids)):
            raise ValueError("Transport subset chemistry does not match its whole reaction records")
        # Reviews remain bound to the complete original reviewed dataset. Keep
        # their hashes/currentness; these counts describe only this transport part.
        chemistry["counts"] = dict(reactions=len(selected),
            compounds=sum(len(r["compounds"]) for r in selected),
            reviewed_reactions=sum(bool(r["review_current"]) for r in selected),
            ready_reactions=sum(bool(r["ready"]) for r in selected))
        chemistry["ready"] = bool(selected) and all(r["ready"] for r in selected)
    validation = subset.get("validation")
    if validation is not None:
        if "chemistry_blockers" in validation:
            validation["chemistry_blockers"] = [b for b in validation["chemistry_blockers"]
                                               if b["experiment_key"] in experiment_keys]
        unresolved = [f for f in subset["review"]
                      if not (f.get("decision") and f["status"] in ("withheld", "unreported"))]
        validation["unresolved_fields"] = len(unresolved)
        if chemistry is not None:
            validation["chemistry_ready"] = chemistry["ready"]
        # Partitioning cannot promote a draft or override run-level source,
        # task, schema or curator restrictions from the original validation.
        if "ready_for_contribution" in validation:
            validation["ready_for_contribution"] = bool(validation["ready_for_contribution"] and
                validation.get("chemistry_ready", True) and not unresolved and
                not validation.get("chemistry_blockers", []))
    for container in (subset, validation):
        if container is not None and "text_boundary_normalizations" in container:
            container["text_boundary_normalizations"] = [change for change in container["text_boundary_normalizations"]
                                                         if change["reaction_id"] in reaction_ids]
    subset["transport_partition"] = note
    return subset


def auxiliary_snapshot(directory, files):
    """Read explicit attachments within this run before exporting or partitioning."""
    result={}
    if len(files or []) > MAX_AUXILIARY_FILES:
        raise ValueError('At most 64 auxiliary files are supported by the contribution interface')
    reserved={'extraction.xlsx','extraction-audit.json','extraction-audit.json.gz',
              'dataset.json','validation.json','receipt.json','workbook-plan.json',
              'chemistry-report.json','text-boundary-normalizations.json'}
    for filename in files or []:
        path=Path(filename).resolve(strict=True)
        if not path.is_relative_to(directory.resolve()) or not path.is_file():
            raise ValueError('Auxiliary attachments must be files inside this run directory')
        if path.name in reserved or path.name in result or path.name.startswith('.'):
            raise ValueError('Duplicate or reserved auxiliary attachment filename')
        if path.stat().st_size>5*1024*1024:
            raise ValueError('Auxiliary attachment exceeds the 5 MiB limit; partition losslessly before exporting')
        body=path.read_bytes()
        if len(body)>5*1024*1024:
            raise ValueError('Auxiliary attachment changed size during snapshot')
        result[path.name]=body
    return result


def excel_backend_limits(environment=None):
    """Explicit finite child-process budgets; large audits need an 8 GiB heap."""
    env = os.environ if environment is None else environment
    result = {}
    for name, default, low, high in [("CMCCDB_XLSX_HEAP_MIB", 8192, 512, 8192),
                                     ("CMCCDB_XLSX_TIMEOUT_SECONDS", 600, 30, 1800)]:
        try:
            value = int(env.get(name, default))
        except (ValueError, TypeError) as error:
            raise ValueError(f"{name} must be an integer between {low} and {high}") from error
        if not low <= value <= high:
            raise ValueError(f"{name} must be an integer between {low} and {high}")
        result[name] = value
    return result


def export_run(pipeline, run, assembled, allow_partial, *, auxiliary_files=None, _auxiliary_blobs=None, _audit_body=None, _audit_transport=None, _split=True):
    directory = pipeline.store.run_dir(run)
    auxiliary = _auxiliary_blobs if _auxiliary_blobs is not None else auxiliary_snapshot(directory,auxiliary_files)
    outputs = directory / "exports"
    outputs.mkdir(exist_ok=True)
    # Every export gets a fresh revision; existing human-edited workbooks survive.
    revision = Path(tempfile.mkdtemp(prefix="revision-", dir=outputs))
    try:
        assembled = copy.deepcopy(assembled)
        assembled["dataset"], final_text_changes = export_text_boundaries(assembled["dataset"])
        text_changes = assembled.get("text_boundary_normalizations", []) + final_text_changes
        if text_changes:
            note = (f"Export removed boundary whitespace in {len(text_changes)} text cells to match the existing XLSX converter. "
                    "Internal text and all numerical values are unchanged; original worker values remain in the audit.")
            assembled["validation"]["warnings"].append(note)
            with pipeline.store.connect(write=True) as con:
                pipeline.store.event(con, run, "export_text_boundaries_normalized", dict(
                    revision=revision.name, reason=note, changes=text_changes))
        (revision / "text-boundary-normalizations.json").write_text(canonical(text_changes), encoding="utf-8")
        audit_body = _audit_body if _audit_body is not None else canonical(pipeline.store.audit(run)).encode("utf-8")
        audit_transport = prepare_audit_transport(revision, audit_body, snapshot=_audit_transport)
        assembled['audit_transport'] = {key:value for key,value in audit_transport.items()
                                       if key not in ('blobs', 'archival_gzip')}
        if set(auxiliary) & set(audit_transport['files']):
            raise ValueError('Auxiliary attachment collides with audit transport filename')
        auxiliary_file_count = len(auxiliary) + len(audit_transport['files'])
        if auxiliary_file_count > MAX_AUXILIARY_FILES:
            raise ValueError('Complete audit and auxiliary attachments exceed the 64-file contribution limit; '
                             'no evidence was omitted and workbook splitting cannot reduce shared provenance')
        auxiliary_bytes = sum(len(blob) for blob in audit_transport['blobs'].values()) + sum(map(len, auxiliary.values()))
        if auxiliary_bytes >= MAX_REQUEST_BYTES:
            raise ValueError('Complete audit and auxiliary attachments exceed the 30 MiB request budget before XLSX; '
                             'no evidence was omitted and workbook splitting cannot reduce shared provenance')
        plan = workbook_plan(pipeline, run, assembled, allow_partial=allow_partial)
        (revision / "workbook-plan.json").write_text(canonical(plan), encoding="utf-8")
        for name,body in auxiliary.items():
            (revision/name).write_bytes(body)
        (revision / "dataset.json").write_text(canonical(assembled["dataset"]), encoding="utf-8")
        (revision / "validation.json").write_text(canonical(assembled["validation"]), encoding="utf-8")
        if "chemistry_report" in assembled:
            (revision / "chemistry-report.json").write_text(canonical(assembled["chemistry_report"]), encoding="utf-8")
        node = pipeline.node or os.environ.get("CMCCDB_XLSX_NODE") or shutil.which("node")
        if not node:
            raise ValueError("Excel export requires a configured Node/Artifact Tool backend")
        script = Path(__file__).with_name("write_workbook.mjs")
        limits = excel_backend_limits()
        try:
            proc = subprocess.run([str(node), f"--max-old-space-size={limits['CMCCDB_XLSX_HEAP_MIB']}",
                str(script), str(revision / "workbook-plan.json"), str(revision / "extraction.xlsx")],
                capture_output=True, text=True, timeout=limits['CMCCDB_XLSX_TIMEOUT_SECONDS'])
        except subprocess.TimeoutExpired as error:
            raise ValueError(f"Excel backend exceeded its configured {limits['CMCCDB_XLSX_TIMEOUT_SECONDS']} second limit; export withheld") from error
        if proc.returncode:
            detail = proc.stderr if len(proc.stderr) <= 3000 else proc.stderr[:1500] + "\n[diagnostics shortened]\n" + proc.stderr[-1500:]
            raise ValueError("Excel backend failed: " + detail)
        original = dataset_from_json(assembled["dataset"])
        token = original.dataset_id.removeprefix("cmcc_dataset-")
        actual = dataset_constructor.DatasetConstructor.enumerate_spreadsheet(
            str(revision / "extraction.xlsx"), name=original.name, id=token)
        # The constructor does not carry a Dataset description from Excel.
        actual.description = original.description
        # Header grouping may reorder experiments. Match by persistent reaction ID.
        if (len(original.reactions) != len(actual.reactions) or
            len({r.reaction_id for r in actual.reactions}) != len(actual.reactions) or
            {r.reaction_id: r for r in original.reactions} != {r.reaction_id: r for r in actual.reactions}):
            # Keep the mismatched bytes and expected/actual data for diagnosis;
            # they have no successful receipt and cannot enter a upload batch.
            (revision / "round-trip-actual.json").write_text(canonical(
                json_format.MessageToDict(actual, preserving_proto_field_name=True)), encoding="utf-8")
            raise ValueError("XLSX round-trip changed scientific data; export withheld")
        # Match the interface's current per-file upload limit. Archive the
        # complete audit; never truncate evidence to fit an upload silently.
        upload_errors = [f"{name} exceeds the current 5 MiB per-file contribution limit"
                         for name in ["extraction.xlsx", *audit_transport['files'], *auxiliary]
                         if (revision / name).stat().st_size > MAX_ATTACHMENT_BYTES]
        request_bytes = (revision / 'extraction.xlsx').stat().st_size + auxiliary_bytes
        if request_bytes > MAX_REQUEST_BYTES:
            upload_errors.append('Workbook plus complete audit and auxiliaries exceeds the 30 MiB request budget')
        parts = []
        if upload_errors and _split:
            # Preserve the coherent full-paper workbook. Produce additional,
            # independently re-imported XLSX files with disjoint whole records.
            # Every part carries the same complete audit snapshot, including
            # candidates and withheld claims outside that part's record set.
            source_reactions = assembled["dataset"]["reactions"]
            if len(source_reactions) > 1:
                by_id = {e["reaction_id"]: e["key"] for e in pipeline.store.audit(run)["experiments"]}
                pending = [source_reactions[:len(source_reactions)//2], source_reactions[len(source_reactions)//2:]]
                while pending:
                    group = pending.pop(0)
                    ids = [r["reaction_id"] for r in group]
                    note = (f"Whole-record subset of {len(source_reactions)} reviewed records in {run}. "
                            "Use every transport part in the full export receipt. The complete paper workbook and "
                            "the unchanged complete audit snapshot are preserved. Record IDs: " + ", ".join(ids))
                    subset = transport_subset(assembled, group, {by_id[i] for i in ids}, note)
                    part = export_run(pipeline, run, subset, True, _audit_body=audit_body, _auxiliary_blobs=auxiliary,
                                      _audit_transport=audit_transport, _split=False)
                    if part["upload_size_checks_passed"]:
                        part["reaction_ids"] = ids
                        parts.append(part)
                    elif len(group) > 1:
                        middle = len(group)//2
                        pending[0:0] = [group[:middle], group[middle:]]
                    else:
                        raise ValueError("One whole record exceeds the upload limit; its scientific fields and evidence were preserved")
                expected_ids = [r["reaction_id"] for r in source_reactions]
                actual_ids = [i for p in parts for i in p["reaction_ids"]]
                if len(actual_ids) != len(set(actual_ids)) or sorted(actual_ids) != sorted(expected_ids):
                    raise ValueError("Transport partition lost or duplicated whole records")
        if upload_errors and not parts and not allow_partial:
            raise ValueError("; ".join(upload_errors) + "; split this extraction into smaller reviewed runs")
        validation = copy.deepcopy(assembled["validation"])
        validation["warnings"].extend(upload_errors)
        if upload_errors:
            validation["ready_for_contribution"] = False
        (revision / "validation.json").write_text(canonical(validation), encoding="utf-8")
        result = dict(workbook=str(revision / "extraction.xlsx"), audit=str(revision / "extraction-audit.json"),
                      audit_attachment=str(revision / "extraction-audit.json.gz"),
                      dataset=str(revision / "dataset.json"), validation=validation,
                      round_trip_verified=True, reactions=len(actual.reactions), partial=allow_partial,
                      auxiliary_files=[*audit_transport['files'], *auxiliary],
                      auxiliary_sha256={name:hashlib.sha256(body).hexdigest()
                                        for name,body in {**audit_transport['blobs'], **auxiliary}.items()},
                      audit_transport_files=audit_transport['files'],
                      audit_transport_manifest=audit_transport['manifest'],
                      audit_json_sha256=audit_transport['json_sha256'],
                      audit_gzip_sha256=audit_transport['gzip_sha256'],
                      upload_size_checks_passed=not upload_errors,
                      transport_ready=not upload_errors or bool(parts),
                      upload_request_bytes=request_bytes,
                      upload_request_budget_bytes=MAX_REQUEST_BYTES,
                      shared_auxiliary_bytes=auxiliary_bytes,
                      auxiliary_file_count=auxiliary_file_count,
                      upload_auxiliary_file_limit=MAX_AUXILIARY_FILES,
                      transport_parts=parts,
                      text_boundary_normalizations=text_changes,
                      workbook_sha256=hashlib.sha256((revision / "extraction.xlsx").read_bytes()).hexdigest())
        (revision / "receipt.json").write_text(canonical(result), encoding="utf-8")
        with pipeline.store.connect(write=True) as con:
            pipeline.store.event(con, run, "workbook_exported", result)
        return result
    except BaseException as error:
        failed = directory / "failed-exports"
        failed.mkdir(exist_ok=True)
        (revision / "failure.json").write_text(canonical(dict(error=str(error))), encoding="utf-8")
        shutil.move(str(revision), str(failed / revision.name))
        raise
