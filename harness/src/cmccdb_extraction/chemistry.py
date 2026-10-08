"""Real RDKit checks plus explicit, claim-bound human/model chemistry review.

Parsing does not establish the correct source identity, salt, stereoisomer,
coordination graph or reaction. Reviews never generate or repair structures.
"""
import copy
import hashlib
import math
import re
from functools import lru_cache
from .schema import canonical, escaped

REVIEW_VERSION = 1
STRUCTURE_TYPES = {"SMILES", "CXSMILES"}


def sha(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def base_dataset(dataset):
    result = copy.deepcopy(dataset)
    for reaction in result.get("reactions", []):
        reaction.get("provenance", {}).get("reaction_metadata", {}).pop("chemistry_review", None)
    return result


def compounds(reaction):
    for key, inp in reaction.get("inputs", {}).items():
        if not isinstance(inp, dict):
            yield f"/inputs/{escaped(key)}", inp
            continue
        for number, compound in enumerate(inp.get("components", [])):
            yield f"/inputs/{escaped(key)}/components/{number}", compound
    for oi, outcome in enumerate(reaction.get("outcomes", [])):
        if not isinstance(outcome, dict):
            yield f"/outcomes/{oi}", outcome
            continue
        for pi, product in enumerate(outcome.get("products", [])):
            yield f"/outcomes/{oi}/products/{pi}", product
    # Solvents or other molecular workup components require structure review too.
    for wi, workup in enumerate(reaction.get("workups", [])):
        if not isinstance(workup, dict):
            yield f"/workups/{wi}", workup
            continue
        for ri, reagent in enumerate(workup.get("input", {}).get("components", [])):
            yield f"/workups/{wi}/input/components/{ri}", reagent


def formula_counts(text):
    """Parse bounded conventional formulas, including hydrates and parentheses.

    Formal charge is checked separately. Unsupported conventions fail explicitly.
    """
    if not text or len(text) > 500:
        raise ValueError("Expected a bounded nonempty source formula")
    text = text.replace(" ", "").replace("·", ".")
    text = re.sub(r"[+-](?:\d+)?$", "", text)
    total = {}
    for part in text.split("."):
        match = re.match(r"^(\d+)(?=[A-Z(])", part)
        multiplier = int(match[1]) if match else 1
        if match:
            part = part[match.end():]
        items = re.findall(r"[A-Z][a-z]?|\d+|[()]", part)
        if "".join(items) != part or len(items) > 500:
            raise ValueError("Unsupported source formula convention; review it explicitly")
        stack = [{}]
        pos = 0
        while pos < len(items):
            item = items[pos]; pos += 1
            if item == "(":
                stack.append({})
                if len(stack) > 20:
                    raise ValueError("Source formula nesting exceeds limit")
                continue
            if item == ")":
                if len(stack) == 1:
                    raise ValueError("Unbalanced source formula")
                group = stack.pop()
                factor = int(items[pos]) if pos < len(items) and items[pos].isdigit() else 1
                if pos < len(items) and items[pos].isdigit(): pos += 1
                for element, count in group.items():stack[-1][element] = stack[-1].get(element, 0) + factor*count
                continue
            if item.isdigit():
                raise ValueError("Unexpected source formula coefficient")
            count = int(items[pos]) if pos < len(items) and items[pos].isdigit() else 1
            if pos < len(items) and items[pos].isdigit(): pos += 1
            if count < 1 or count > 100000:
                raise ValueError("Source formula atom count exceeds limit")
            stack[-1][item] = stack[-1].get(item, 0) + count
        if len(stack) != 1 or not stack[0]:raise ValueError("Unbalanced or empty source formula")
        for element, count in stack[0].items():total[element] = total.get(element, 0) + multiplier*count
    return total


@lru_cache(maxsize=4096)
def inspect_smiles(value, identifier_type="SMILES"):
    if not isinstance(value, str) or not value.strip() or len(value) > 100000:
        return dict(valid=False, errors=["SMILES is empty or exceeds the bounded structure limit"])
    try:
        from rdkit import Chem, rdBase
        from rdkit.Chem import rdMolDescriptors
        params = Chem.SmilesParserParams()
        params.sanitize = True
        params.parseName = False
        params.allowCXSMILES = identifier_type == "CXSMILES"
        # Silence only parser diagnostics; errors remain explicit in the report.
        with rdBase.BlockLogs():
            mol = Chem.MolFromSmiles(value, params)
        if mol is None or mol.GetNumAtoms() == 0:
            return dict(valid=False, rdkit_version=rdBase.rdkitVersion, errors=["RDKit could not parse and sanitize the complete molecular graph"])
        Chem.AssignStereochemistry(mol, cleanIt=True, force=True)
        centers = Chem.FindMolChiralCenters(mol, includeUnassigned=True, useLegacyImplementation=False)
        stereo_bonds = sum(b.GetStereo() not in (Chem.BondStereo.STEREONONE, Chem.BondStereo.STEREOANY) for b in mol.GetBonds())
        return dict(valid=True, rdkit_version=rdBase.rdkitVersion,
                    canonical_isomeric_smiles=Chem.MolToSmiles(mol, isomericSmiles=True),
                    formula=rdMolDescriptors.CalcMolFormula(mol, separateIsotopes=True, abbreviateHIsotopes=True), formal_charge=Chem.GetFormalCharge(mol),
                    fragments=len(Chem.GetMolFrags(mol)), atoms=mol.GetNumAtoms(),
                    assigned_stereo_centers=sum(label != "?" for _, label in centers),
                    unassigned_stereo_centers=sum(label == "?" for _, label in centers),
                    assigned_stereo_bonds=stereo_bonds,
                    dummy_atoms=sum(atom.GetAtomicNum() == 0 for atom in mol.GetAtoms()),
                    radical_electrons=sum(atom.GetNumRadicalElectrons() for atom in mol.GetAtoms()),
                    charged_atoms=sum(atom.GetFormalCharge() != 0 for atom in mol.GetAtoms()),
                    errors=[])
    except ImportError:
        return dict(valid=False, errors=["Real RDKit is unavailable; structure validation is required"])
    except Exception as error:
        return dict(valid=False, errors=["RDKit structure validation failed: " + str(error)])


def compound_report(path, compound, review=None):
    errors = []
    if not isinstance(compound, dict):
        return dict(compound_path=path, names=[], status="unresolved", structures=[], crystal_parameters={},
                    errors=["Incomplete compound slot; no structure can be reviewed"], ready=False)
    identifiers = compound.get("identifiers", [])
    if not isinstance(identifiers, list):
        errors.append("Compound identifiers must be a contiguous array")
        identifiers = []
    if any(not isinstance(i, dict) for i in identifiers):
        errors.append("Incomplete identifier slot; preserve array indices and repair the missing identity claim")
    names = [i.get("value", "") for i in identifiers if isinstance(i, dict) and i.get("type") == "NAME"]
    structures = [dict(identifier_index=n, source_value=i.get("value", ""), **inspect_smiles(i.get("value", ""), i["type"]))
                  for n, i in enumerate(identifiers) if isinstance(i, dict) and i.get("type") in STRUCTURE_TYPES]
    for structure in structures:
        errors.extend(structure["errors"])
        if structure.get("dummy_atoms"):
            errors.append("Wildcard atoms leave the molecular graph unresolved")
    valid = [s for s in structures if s["valid"]]
    if len({s["canonical_isomeric_smiles"] for s in valid}) > 1:
        errors.append("SMILES identifiers disagree in graph, charge, components or stereochemistry")
    crystal = compound.get("crystal_parameters", {})
    if not isinstance(crystal, dict):
        errors.append("Incomplete crystal parameters message")
        crystal = {}
    db = crystal.get("database_identifier", {})
    if not isinstance(db, dict):
        errors.append("Incomplete crystal database identifier")
        db = {}
    if crystal:
        for axis in ("a", "b", "c"):
            if axis in crystal and (not isinstance(crystal[axis], dict) or not crystal[axis].get("units") or not isinstance(crystal[axis].get("value"), (int,float)) or not math.isfinite(crystal[axis]["value"]) or crystal[axis]["value"] <= 0):
                errors.append("Crystal cell length requires a positive source value and explicit units: " + axis)
        for axis in ("alpha", "beta", "gamma"):
            if axis in crystal:
                value = crystal[axis].get("value", 0) if isinstance(crystal[axis], dict) else 0
                unit = crystal[axis].get("units") if isinstance(crystal[axis], dict) else None
                ceiling = {"DEGREES":180, "RADIANS":3.141592653589793}.get(unit)
                if ceiling is None or not isinstance(value, (int,float)) or not 0 < value < ceiling:
                    errors.append("Crystal angle requires source-supported units and a value strictly between 0 and 180 degrees: " + axis)
        if db and not db.get("value", "").strip():errors.append("Crystal database identifier has no source value")
    status = review["status"] if review else "unreviewed"
    if not review:
        errors.append("Explicit compound chemistry review/status/reason is missing")
        if not structures:errors.append("No SMILES; molecular identity or an explicit periodic/mixture exception needs review")
    else:
        for name, check in review["checks"].items():
            if check["status"] == "needs_revision":errors.append("Manager chemistry check needs revision: " + name)
        if review["checks"]["identity"]["status"] != "approved":
            errors.append("Source chemical identity must be approved explicitly")
        if status == "resolved_molecular":
            for name in ("connectivity", "formula", "charge"):
                if review["checks"][name]["status"] != "approved":errors.append("Molecular chemistry check must be approved: " + name)
            if not valid:errors.append("Resolved molecular compound requires a valid source-supported SMILES")
            if valid:
                structure = valid[0]
                if review.get("expected_formula"):
                    try:
                        if formula_counts(structure["formula"]) != formula_counts(review["expected_formula"]):errors.append("SMILES formula disagrees with the cited source formula")
                    except ValueError as error:errors.append(str(error))
                if review.get("expected_formal_charge") is not None and structure["formal_charge"] != review["expected_formal_charge"]:
                    errors.append("SMILES formal charge disagrees with the cited source charge")
                if structure["assigned_stereo_centers"] < review.get("required_stereo_centers", 0):errors.append("Source-specified stereocenters are missing from the SMILES")
                if structure["assigned_stereo_bonds"] < review.get("required_stereo_bonds", 0):errors.append("Source-specified bond stereochemistry is missing from the SMILES")
        elif status in ("periodic_crystal", "nonmolecular_material", "mixture"):
            if status == "nonmolecular_material":
                # Narrow exception for source-identified elemental liquid mercury.
                # [Hg] is a composition token, never a discrete-molecule/lattice claim.
                if review.get("structure_scope") != "elemental_composition" or not valid:
                    errors.append("Nonmolecular liquid mercury requires a valid elemental composition token")
                if valid and (valid[0]["formula"] != "Hg" or valid[0]["atoms"] != 1 or valid[0]["formal_charge"] != 0):
                    errors.append("Nonmolecular material exception currently supports only neutral elemental mercury")
                if crystal or review["crystal_reference_status"] != "not_applicable":
                    errors.append("Nonmolecular liquid mercury must not invent a crystal reference or cell")
            if structures:
                scope = review.get("structure_scope", "molecular_graph")
                if status not in ("periodic_crystal", "nonmolecular_material") or scope not in ("stoichiometric_formula_unit", "elemental_composition"):
                    errors.append("Periodic/mixture exception must not assert a made-up finite SMILES graph")
                elif not review.get("representation_limit", "").strip() or not review.get("expected_formula"):
                    errors.append("Periodic composition token requires explicit representation limits and a source-reviewed nominal formula")
                elif valid:
                    structure = valid[0]
                    try:
                        if formula_counts(structure["formula"]) != formula_counts(review["expected_formula"]):
                            errors.append("Periodic token formula disagrees with the cited source composition")
                    except ValueError as error:errors.append(str(error))
                    if review.get("expected_formal_charge") is not None and structure["formal_charge"] != review["expected_formal_charge"]:
                        errors.append("Periodic token formal charge disagrees with the reviewed composition")
                    if scope == "elemental_composition" and structure["atoms"] != 1:
                        errors.append("Elemental composition token must contain exactly one source element atom")
                    if scope == "stoichiometric_formula_unit" and structure["fragments"] == structure["atoms"] and structure["radical_electrons"] and not structure["charged_atoms"]:
                        errors.append("An unbonded neutral atom bag is not a supported ionic or molecular stoichiometric formula unit")
                for name in ("formula", "charge"):
                    if review["checks"][name]["status"] != "approved":
                        errors.append("Periodic token chemistry check must be approved: " + name)
            if not review.get("exception_components_resolved"):
                errors.append("Exception composition/identity is unresolved; contribution-ready is prohibited")
            if status == "periodic_crystal" and review["crystal_reference_status"] == "not_applicable":
                errors.append("Periodic solid must report an actual crystal reference or its source absence")
        else:
            errors.append("Unresolved chemical graph cannot be contribution-ready")
        if review["crystal_reference_status"] == "present" and not db:
            errors.append("Review claims a crystal reference, but none was extracted")
        if db and review["crystal_reference_status"] != "present":
            errors.append("Extracted crystal reference has not been source-reviewed")
    return dict(compound_path=path, names=names, status=status, structures=structures,
                structure_scope=review.get("structure_scope", "molecular_graph") if review else "unreviewed",
                representation_limit=review.get("representation_limit", "") if review else "",
                crystal_parameters=crystal, errors=errors, ready=not errors)


def claim_snapshot(fields):
    return sha([dict(path=f["path"], status=f["status"], selected_claim_id=f.get("selected_claim_id"),
                     value=f.get("value"), candidate_ids=sorted(c["claim_id"] for c in f["candidates"]))
                for f in sorted(fields, key=lambda f:f["path"])])


def chemistry_report(dataset, fields, experiment_by_reaction_id, reviews=None):
    base = base_dataset(dataset);dataset_sha = sha(base);reviews = reviews or {};reactions=[]
    for reaction in base.get("reactions", []):
        key = experiment_by_reaction_id[reaction["reaction_id"]]
        snapshot = dict(dataset_sha256=dataset_sha, reaction_sha256=sha(reaction),
                        claims_sha256=claim_snapshot([f for f in fields if f["experiment_key"] == key]))
        review = reviews.get(key);errors=[]
        current = bool(review and all(review[k] == value for k,value in snapshot.items()))
        if not review:errors.append("Manager reaction chemistry review is missing")
        elif not current:errors.append("Manager chemistry review is stale: claims or assembled dataset changed")
        elif review["disposition"] != "approved":errors.append("Manager reaction chemistry review requires revision")
        if current:
            for name in ("reaction_plausibility", "measurement_semantics"):
                if review[name]["status"] != "approved":errors.append("Reaction check has not been approved: " + name)
        supplied = {c["compound_path"]:c for c in review["compounds"]} if current else {}
        reports = [compound_report(path,compound,supplied.get(path)) for path,compound in compounds(reaction)]
        selected_fields = [f for f in fields if f["experiment_key"] == key and f.get("selected_claim_id") and f["status"] in ("agreed", "reviewed")]
        for report in reports:
            path = report["compound_path"]
            report["selected_identity_claim_ids"] = sorted({f["selected_claim_id"] for f in selected_fields
                if f["path"].startswith(path + "/identifiers/") or f["path"] == path + "/identifiers" or f["path"].startswith(path + "/crystal_parameters/") or f["path"] == path + "/crystal_parameters" or f["path"] == path or path.startswith(f["path"] + "/")})
        if not reports:errors.append("No identifiable compound graph inventory is present")
        if current and set(supplied) != {r["compound_path"] for r in reports}:errors.append("Manager compound review inventory does not match the current compound paths")
        errors += [r["compound_path"] + ": " + error for r in reports for error in r["errors"]]
        reactions.append(dict(experiment_key=key, reaction_id=reaction["reaction_id"], **snapshot,
                              compounds=reports, review_current=current, review_actor=review.get("actor") if review else None,
                              errors=errors, ready=not errors))
    return dict(review_version=REVIEW_VERSION, dataset_sha256=dataset_sha,
                ready=bool(reactions) and all(r["ready"] for r in reactions),
                counts=dict(reactions=len(reactions), compounds=sum(len(r["compounds"]) for r in reactions),
                            reviewed_reactions=sum(r["review_current"] for r in reactions),
                            ready_reactions=sum(r["ready"] for r in reactions)), reactions=reactions,
                meaning="RDKit validity is a technical check, never chemical approval. Source-grounded current manager review is mandatory.")
