"""Provider-independent task routing recommendations for the external MCP host."""

from .schema import field_at, tokens

ROLE_BY_COMPLEXITY = {"routine": "light", "contextual": "standard", "expert": "expert"}
ESCALATION_TRIGGERS = ["ambiguous_source", "missing_context", "worker_disagreement",
                       "structure_uncertainty", "unsupported_field", "failed_validation"]

DISPATCH_INSTRUCTIONS = """Mandatory manager dispatch step:
Before delegating, classify every task with dispatch.complexity, an evidence-based
dispatch.reason and dispatch.required_capabilities. Split mixed-complexity work
so routine transcription does not inherit a whole paper's expert classification.
Routine -> light worker: direct transcription from clearly labelled text or a
previously interpreted table, explicit amounts/conditions and metadata, preserving
source citations and units. Contextual -> standard worker: connect procedures to
tables and SI, interpret footnotes, distinguish experiments and measurement types.
Expert -> expert worker: interpret chemical structures/SMILES, stereochemistry,
salts, coordination, crystallography, ambiguous transformations and disagreements.
Volume alone is not complexity: large regular tables can be split into routine
tasks after an expert establishes their meaning. Vision is a separate capability;
a light worker may read a simple table image if its model supports vision.

Use get_run_status task.dispatch recommendations before starting worker contexts.
Map light/standard/expert roles to actual available model revisions in the MCP
host. Pick the least expensive role meeting the recommendation and capabilities;
do not use the expert model for every task. If that role is unavailable, use the
least expensive capable higher role and record the fallback reason in dispatch_note
when leasing. Never downgrade complex chemistry to meet a cost target.
Keep at least two independent workers per task at the required level. A light
worker must report ambiguity or missing context instead of guessing; route the
remaining interpretation and chemical approval to expert review. Worker agreement
alone is not approval. If a changed scope or new claim is needed after completed
submissions, create a revised run: the existing plan and submissions are immutable.
The MCP host launches and selects models. Routing recommendations and recorded
model revisions do not independently verify model capability, price or compliance.
"""


def dispatch_policy():
    return dict(roles=dict(ROLE_BY_COMPLEXITY), unclassified_complexity="contextual",
                escalation_triggers=list(ESCALATION_TRIGGERS),
                model_selection="External MCP host maps roles to available models",
                runtime_enforcement=False,
                final_chemistry_review_role="expert")


def _includes_structures(desc, seen=None):
    if desc is None:
        return False
    if desc.name in {"CompoundIdentifier", "CrystalParameters"}:
        return True
    seen = set() if seen is None else seen
    if desc.full_name in seen:
        return False
    seen.add(desc.full_name)
    return any(_includes_structures(f.message_type, seen) for f in desc.fields)


def recommend_dispatch(task):
    """Conservatively route unclassified/structure scopes without changing the plan."""
    declared = task.get("dispatch")
    complexity = declared["complexity"] if declared else "contextual"
    capabilities = set(declared.get("required_capabilities", [])) if declared else set()
    capabilities.add("text")
    if task.get("modality", "text") in {"vision", "mixed"}:
        capabilities.add("vision")
    guards = []
    for path in task["allowed_paths"]:
        field = field_at(path, allow_container=True)
        if ("identifiers" in tokens(path) or "crystal_parameters" in tokens(path)
                or _includes_structures(field.message_type)):
            guards.append(f"Structure/crystal field scope requires expert interpretation: {path}")
    if guards or "chemistry" in capabilities:
        effective = "expert"
        capabilities.add("chemistry")
    else:
        effective = complexity
    return dict(classified=declared is not None, assigned_complexity=complexity,
                effective_complexity=effective, preferred_worker_role=ROLE_BY_COMPLEXITY[effective],
                required_capabilities=sorted(capabilities),
                reason=declared["reason"] if declared else
                    "Unclassified legacy task: use standard reasoning until the manager assesses it.",
                scope_guardrails=guards, escalation_triggers=list(ESCALATION_TRIGGERS),
                runtime_enforcement=False)
