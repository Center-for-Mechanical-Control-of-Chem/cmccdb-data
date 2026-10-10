# Dispatch extraction tasks by complexity

The manager plans and reviews the extraction, and chooses an appropriate worker
role for each task. The MCP host maps these roles to actual available models and
starts independent worker contexts. The server provides routing instructions,
structured task labels, recommendations and audit records. It does not launch
models or verify their capabilities or prices; capabilities report
`dispatch_policy.runtime_enforcement=false` so this boundary is explicit.

## Routing rubric

| Task complexity | Preferred worker role | Typical work |
| --- | --- | --- |
| `routine` | `light` | Copy a clearly labelled quantity, condition, author name or DOI; transcribe a regular table whose meaning was already established. |
| `contextual` | `standard` | Connect a procedure to rows in a table or SI, interpret footnotes, distinguish measurements from separate experiments. |
| `expert` | `expert` | Interpret structures and SMILES, stereochemistry, salts/hydrates, coordination, crystallography, ambiguous chemistry and disagreements. |

Use task complexity together with required capabilities. Vision is separate:
a simple table image can go to a light vision-capable worker, while a chemical
scheme still needs expert interpretation. A task's volume alone does not imply
expert complexity. After the manager establishes a large table's semantics,
partition its routine row transcription into small independently checked tasks.
Light workers handle only their assigned fields. Dedicated expert tasks resolve
compound structures and crystals; the manager checks chemistry for the assembled
run. Routine workers should flag identity ambiguity without taking on a full
chemical interpretation outside their field scope.

## Describe the route in the plan

Each newly planned task should include a `dispatch` object. For example:

```json
{
  "task_id": "frequency_rows",
  "experiment_keys": ["experiment-1"],
  "allowed_paths": ["/conditions/mechanochemistry/frequency"],
  "evidence_ids": ["REPLACE_WITH_REAL_EVIDENCE_ID"],
  "instructions": "Copy each explicitly reported milling frequency with its original unit and exact citation.",
  "modality": "text",
  "required_workers": 2,
  "dispatch": {
    "complexity": "routine",
    "reason": "The table has an unambiguous frequency column and the manager has verified the applicable method.",
    "required_capabilities": ["text"]
  }
}
```

`complexity` must be `routine`, `contextual` or `expert`; `reason` must explain
the classification. Capabilities may be `text`, `vision` or `chemistry`. The
reason should refer to actual source context. For figure-dependent work, use
`modality=vision` or `mixed`; routing then includes the vision requirement.

Before starting workers, call `get_run_status(run_id)` and read each task's
derived `dispatch` recommendation. Broad compound/outcome scopes, identifier
fields and crystal information raise the recommendation to expert even when the
manager labelled them routine. Split those tasks into narrow amount/condition
paths and separate expert identity/structure tasks to obtain appropriate routes.
These guardrails affect recommendations; they do not prevent an unsuitable
model from acquiring a lease.

Older tasks without dispatch metadata remain resumable. They default to standard
reasoning, with the same conservative structure/crystal guardrails, and report
`classified=false`. A plan remains immutable. Classification advice does not
rewrite the scientific scope, accepted claims or existing plan.

## Configure the host's model roles

Choose the actual models in the MCP host's delegation configuration. This example
describes the intended mapping; it is not a provider-specific configuration file:

```text
manager: a model capable of planning and source-aware scientific review
light: your lowest-cost model validated for direct scientific transcription
standard: a model validated for connecting procedures, tables and SI
expert: a model validated for structural chemistry and ambiguous scientific interpretation
```

A model may fill multiple roles if it meets their requirements. Obtain separate
contexts for the independent workers; two names attached to the same extraction
context are not independent evidence. Check capability against reference papers,
rather than treating size or price as proof of extraction quality. Keep final
source-aware chemistry review at the expert level even when transcription uses
light models.

Use the least expensive available worker meeting the recommended role and
capabilities. If the preferred role is unavailable, choose a capable higher role
and pass the reason as `dispatch_note` to `lease_extraction_task`. Record the
actual model revision rather than the role name. A successful lease records the
recommendation, exact instructions, model revision and dispatch note in its
audit event. The server does not resolve provider model names or enforce this
mapping; the host performs those actions.

If all roles map to one expensive model, adding task labels alone cannot save
inference cost. Configure the light role and route routine work to it in the host.

## Escalate uncertainty

Workers should abstain and identify missing context rather than guess when they
encounter ambiguous source text, disagreement, uncertain structures, unsupported
fields or validation failures. The manager sends interpretation to expert review
and adjudicates source-supported existing candidates. If the correction needs
new claims or a different scope after completed submissions, make a revised run;
the current plan and completed submissions remain immutable. This patch does
not introduce automatic retries, new expert leases on completed tasks or a task
mutation API.

Every tier still supplies exact citations, preserves units and follows the
shared chemistry policy. Worker agreement and successful RDKit parsing do not
replace chemical approval. Cost controls must not remove the independent checks
or permit unresolved chemistry in a normal export.

## Copyable manager instruction

```text
Before delegating, classify every task with dispatch.complexity, a source-based
reason and required capabilities. Partition routine transcription from contextual
interpretation and expert chemistry. Call get_run_status to inspect the derived
recommendations, then map light/standard/expert roles to the host's available
models. Use light workers for routine tasks, standard workers for contextual
tasks, and expert workers for structures and scientific interpretation. Require
vision separately where needed. Use at least two independent contexts per task.
Record the actual model revision and any higher-tier fallback reason in the lease.
Escalate ambiguity and disagreements to expert review. Keep the existing SMILES,
crystal, evidence, chemistry-review and final-export requirements at every tier.
```

The shared instructions are delivered at MCP initialization, in both role prompts
and in every worker lease. After applying the patch, restart the MCP processes
and reconnect the clients. `get_capabilities().agent_policy.version` should be
`cmccdb-chemistry-dispatch-2026-10-10`.
