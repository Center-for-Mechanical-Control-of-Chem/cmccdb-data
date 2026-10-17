# Extract mechanochemistry papers into Excel with CMCCDB

This harness helps a manager LLM and independent worker LLMs turn a paper and its
supporting information into a CMCCDB spreadsheet. Workers extract cited claims;
the manager resolves disagreements and checks coverage and chemistry. The harness
validates the result against `cmccdb-schema`, exports an editable `.xlsx`, and
re-imports it to check that spreadsheet conversion preserved the scientific data.
An audit attachment records the evidence, model identities and review decisions.

You can use your own papers and an MCP-capable model host. Model execution and
delegation belong to the host: the harness supplies tools and persistent state.
Giving one model two worker names does not create independent model instances.
Use a host that can create separate worker contexts, or separate MCP clients
connected to the same local run directory. Workers reviewing figures need a
model and host that can read the image responses.

The manager classifies tasks by complexity before starting workers. Routine
transcription goes to light workers, contextual interpretation to standard
workers, and structures, crystallography and ambiguous chemistry to expert
workers. Vision is a separate capability requirement. Your MCP host maps these
roles to available models; the server supplies recommendations and audit records.
See [Dispatching workers](DISPATCH.md) for the rubric and host setup.

## 1. Get the harness and matching schema

Clone both repositories into one workspace:

```bash
mkdir cmccdb-extraction-workspace
cd cmccdb-extraction-workspace
git clone https://github.com/Center-for-Mechanical-Control-of-Chem/cmccdb-data.git
git clone https://github.com/Center-for-Mechanical-Control-of-Chem/cmccdb-schema.git
mkdir -p papers runs
```

Use the harness-containing revision of
[cmccdb-data](https://github.com/Center-for-Mechanical-Control-of-Chem/cmccdb-data)
and the matching revision of
[cmccdb-schema](https://github.com/Center-for-Mechanical-Control-of-Chem/cmccdb-schema)
supplied by the maintainer. A default checkout may predate the harness or the
schema fixes. Confirm that `cmccdb-data/harness/pyproject.toml` and
`cmccdb-data/harness/src/cmccdb_extraction/agent_policy.py` exist. If given specific
branches, tags or commits, select those revisions before installing, for example:

```bash
git -C cmccdb-data checkout REPLACE_WITH_HARNESS_REF
git -C cmccdb-schema checkout REPLACE_WITH_MATCHING_SCHEMA_REF
```

The schema checkout provides the protobuf fields, enums, units, validators and
XLSX converter. Cloning `cmccdb-interface` or starting a database is unnecessary
for extraction. Use the supplied generated protobuf files; generating new ones
is part of schema development, rather than harness setup.

This guide describes the chemistry-policy harness with policy version
`cmccdb-chemistry-dispatch-2026-10-13`. Its matching schema includes typed spreadsheet
string handling. Product crystal information requires
`ProductCompound.crystal_parameters`; reported cell lengths in angstroms require
`LengthUnit.ANGSTROM`. The manager checks these capabilities before extracting
fields that need them. Obtain compatible revisions or companion patches from
the maintainer if those checks fail.

Matching schema revisions also support the reserved `#! / Dataset (v1)` comment
rows for dataset name and description. The exporter checks actual parsed
metadata, rather than replacing a lost description during its round-trip test.
`get_capabilities` reports `dataset_metadata_supported` and gates `xlsx_ready`.

An unknown numerical input amount can be represented by a source-grounded
`Amount.unmeasured` annotation with inferred basis and explicit no-imputation
reasoning. A null unreported claim remains an abstention. Do not fabricate mass,
moles or zero to satisfy validation. For source-named mixtures with unknown
constituents, preserve their identities and unresolved composition separately.

## 2. Install the dependencies

You need Python 3.11 or newer, Poppler, an MCP host, and a compatible Node/artifact
runtime for Excel export. Use a dedicated Python environment. The commands below
run from the workspace created above; replace `/path/to/python3.11` with your
Python executable:

```bash
/path/to/python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ./cmccdb-schema
.venv/bin/python -m pip install -e ./cmccdb-data/harness
.venv/bin/python -m pip check
```

Install the local schema first so the harness resolves `cmccdb-schema` from your
checkout. These installs supply the Python MCP SDK, Pydantic, RDKit and the schema
dependencies. Run every Python command through this environment. On Apple
Silicon, use a native ARM Python and verify the compiled dependencies use that
architecture:

```bash
.venv/bin/python -c 'import platform; from rdkit import Chem; print(platform.machine()); assert Chem.MolFromSmiles("CCO") is not None'
```

`platform.machine()` should report `arm64` on Apple Silicon. Linux machines
should report their own native architecture. The examples use macOS/Linux paths;
Windows users should adapt the environment's executable paths.

Install Poppler's `pdftotext` and `pdftoppm` using your platform's package manager.
Ensure both are on the MCP process's PATH, or supply their containing directory
with `--poppler-bin`. Check them in a terminal:

```bash
pdftotext -v
pdftoppm -v
```

### Excel writer requirement

The current XLSX writer uses a separately provisioned Node package,
`@oai/artifact-tool`. The Python installation does not supply this package. The
tested writer uses the Codex bundled artifact runtime; the harness does not
install that runtime or promise that the package is publicly installable.
Obtain a compatible runtime from your environment or the maintainer before
planning an end-to-end Excel extraction.

Set `NODE_PATH` to the directory containing that runtime's `@oai` package folder
and use its compatible Node executable. These are placeholders to replace:

```bash
export NODE_PATH=/absolute/path/to/runtime/node_modules
/absolute/path/to/runtime/node -e 'console.log(require.resolve("@oai/artifact-tool"))'
```

The test should print the resolved module path. Both settings must also reach
the MCP process, as shown below. PDF indexing, claims, consensus and validation
can run without the writer, but `.xlsx` export requires it. Installing a generic
Node executable alone does not satisfy this requirement.

## 3. Add the server to your MCP host

The server uses MCP over **stdio**. Your model host starts the process and
communicates through its input/output streams. There is no HTTP extraction
endpoint or database tunnel to configure. The implementation uses Python MCP
SDK v2; check your host's configuration instructions and the
[official Python SDK documentation](https://py.sdk.modelcontextprotocol.io/v2/).

For hosts using an `mcpServers` JSON configuration, adapt this example. Replace
every `/absolute/path/...` value with a real absolute path on the machine running
the server. JSON does not expand shell variables such as `$PWD` or `~`.

```json
{
  "mcpServers": {
    "cmccdb-extraction": {
      "command": "/absolute/path/to/cmccdb-extraction-workspace/.venv/bin/python",
      "args": [
        "-m", "cmccdb_extraction.server",
        "--paper-root", "/absolute/path/to/cmccdb-extraction-workspace/papers",
        "--work-root", "/absolute/path/to/cmccdb-extraction-workspace/runs",
        "--poppler-bin", "/absolute/path/to/poppler/bin",
        "--node", "/absolute/path/to/runtime/node"
      ],
      "env": {
        "NODE_PATH": "/absolute/path/to/runtime/node_modules"
      }
    }
  }
}
```

Omit `--poppler-bin` and its following value if the tools are already on the
host's PATH. Paper roots must already exist. You can repeat `--paper-root` to
allow additional source directories. The source files remain unchanged; the
server archives copies, indices, claims and exports inside `--work-root`.

All manager and worker connections for a run must use the same absolute
`--work-root`. Multiple stdio server processes can share that directory on one
machine. Use a local filesystem: the SQLite state is unsuitable for sharing
across hosts over NFS. This implementation does not provide a remote multi-host
transport. Keep separate projects in separate work roots when desired.

Reconnect the MCP connection after configuration changes. Ask your model to call
`get_capabilities` before starting extraction. Check the following results:

| Result | What to check |
| --- | --- |
| `pdf_ready` | `true`; otherwise inspect `pdf_errors` and Poppler paths. |
| `rdkit_ready` | `true`; real molecular structure checks are required. |
| `schema_string_patch_present` | `true`; the installed schema has the required typed string converter. |
| `dataset_metadata_supported` | `true`; the installed converter preserves the reserved dataset name/description rows. |
| `xlsx_ready` | `true` for an Excel workflow; inspect `xlsx_error` if false. |
| `chemical_review_required` | `true`. |
| `agent_policy.version` | `cmccdb-chemistry-dispatch-2026-10-10` for this revision. |
| `dispatch_policy` | Routing policy is available; `runtime_enforcement=false` means the host selects and starts models. |
| `product_crystal_field_available` | `true` when products need crystal information. |
| `angstrom_length_unit_available` | `true` when cell lengths need angstrom units. |

The server delivers the chemistry policy at initialization, in role prompts and
in every worker task packet. Your host must pass those instructions to each LLM
context. Restart all server processes and reconnect workers after upgrading;
existing processes keep their loaded code and policy.

## 4. Prepare one paper and its supporting information

Place a main PDF and all relevant SI in an allowed paper root. For example:

```text
papers/
  example-study/
    article.pdf
    supporting-information.pdf
    measurements.xlsx
    methods.tex
    structures.cif
```

The main article must be PDF. SI may be PDF, `.xlsx`, UTF-8 `.tex` or UTF-8 `.cif`. Extract
ZIP archives into the allowed root beforehand and retain the archive/member
identity in the audit notes. Supply only the files belonging to this paper when
creating its run.

PDF text and original page images are both available. Scanned pages and chemical
diagrams need visual reading; there is no automatic OCR or automatic structure
recognition. XLSX SI keeps original sheets, row numbers, cells, formulas and
existing cached results distinct. Formula text and TeX are never executed.
Workbook charts and embedded images need separate visual inspection in the
host; `view_page` provides PDF pixels and does not render XLSX, TeX or CIF sources.

CIF files are archived byte for byte and indexed as original file lines, including
comments, uncertainties and multiline-value delimiters. Use `read_source_page`
with logical `page=1` and follow its `next_offset`, or cite exact `file_line`
locations with `read_evidence`. CIF indexing does not parse chemistry or produce
SMILES: expert workers must interpret the source and verify crystal assignments.
CIF sources must be valid UTF-8, no larger than 20 MiB or 500,000 physical lines.
Invalid or oversized sources fail explicitly; their bytes are never transcoded.
Video evidence and ZIP containers are not indexed directly.

For a corpus, call `list_papers(offset=0, limit=500)` and follow `next_offset`
until it is `null`; `total` always counts all PDFs in the configured roots.
Keep the source inventory unchanged while paging. Discovery lists PDFs, so include
the corresponding native SI paths explicitly when creating each run.

## 5. Ask the manager to run the extraction

Paste the following into your MCP-capable host, replacing the filenames and
curator details. The curator is the person organizing this extraction; paper
authors and experimenters are separate schema attributions.

```text
Use the cmccdb-extraction MCP server to extract
example-study/article.pdf with supporting information:
example-study/supporting-information.pdf,
example-study/measurements.xlsx, example-study/methods.tex and
example-study/structures.cif.

Dataset name: REPLACE_WITH_DATASET_NAME
Curator name: REPLACE_WITH_CURATOR_NAME
Curator email: REPLACE_WITH_CURATOR_EMAIL

First call get_capabilities and confirm this installation can export XLSX.
When discovering a corpus, follow list_papers.next_offset until null.
Create one run for this source bundle and load extraction_manager for its run_id.
Inventory the complete article and SI before installing the immutable search plan.
Account for all experiments, controls, repeated measurements and excluded material.

Classify each task with dispatch.complexity, a source-based reason and required
capabilities. Separate routine transcription from contextual interpretation and
expert chemistry. Before spawning workers, call get_run_status and follow its
derived routing recommendations. Map light/standard/expert roles to the host's
actual available models; require vision separately for image-based work. Use the
least expensive capable role, and record a dispatch_note if a higher role is
needed because the preferred role is unavailable. Escalate ambiguity and
disagreements to expert review. Keep final chemistry review at the expert level.

Delegate each task to at least two genuinely independent worker contexts. Give
each worker the extraction_worker prompt and its lease packet, a unique persistent
worker ID and its actual model revision. Do not share candidate answers between
workers. Use vision-capable workers for chemical schemes, figures and spectra.

Follow the server's chemistry policy. Keep NAME identifiers and provide
source-supported isomeric SMILES/CXSMILES for resolvable compounds, including
reagents, solvents, workup compounds and products. Check product chemistry against
the paper and SI. For inorganic compounds, supply defensible representations and
reported CrystalParameters, preferring verified CSD/CCDC references. Record explicit
limits for mixtures and periodic materials; never invent structures or crystal data.
Preserve reported units, attribution and the distinction between yield, conversion,
purity and phase fraction. Every claim needs an exact source citation.

Resolve disagreements using existing cited candidates. Review each reaction's
current chemistry through get_chemistry_report and review_reaction_chemistry.
Then call validate_extraction. Export with export_workbook only when the run is
ready_for_contribution. If evidence or chemistry remains unresolved, explain the
blockers and offer a clearly labelled partial review draft instead.

Return the workbook, receipt and complete audit/auxiliary attachments, plus a
brief coverage and unresolved-issues report. Leave database submission to me.
```

The host must actually support delegation or you must coordinate separate worker
clients yourself. The prompt cannot add delegation or vision support to a host
that lacks it. A single model without independent contexts can inspect sources
and plan work, but it cannot meet the independent extraction requirement by
inventing extra worker identities.

### What happens during a run

1. The manager calls `create_run` with the article, SI and curator details, then
   reads the sources using `search_evidence`, `read_source_page` and `view_page`.
   `schema_catalog` supplies canonical fields and enums. A creation
   `idempotency_key` can make retries recover the same run.
2. The manager calls `install_search_plan` with the experiment inventory, evidence,
   field scopes, dependencies, required worker counts and task dispatch metadata. Whole-source coverage
   must be established before setting `source_search_complete=true`. The plan is
   immutable; a revised inventory requires a new run.
3. The manager reads each task's derived dispatch recommendation through
   `get_run_status` and starts independent workers with the needed role and
   capabilities. Workers call `lease_extraction_task`, inspect the evidence and return structured
   claims through `submit_extraction`. They receive no peer candidates in the
   lease packet. They can abstain when evidence is missing or ambiguous.
4. The manager calls `inspect_consensus` and resolves conflicting or inferred
   values with `adjudicate_claim` or `adjudicate_claims`. A field selection uses
   an existing candidate ID, with a recorded reviewer and reason.
5. After the scientific claims are settled, the manager obtains current hashes
   from `get_chemistry_report` and calls `review_reaction_chemistry` for every
   reaction. Review includes all compounds, stereochemistry, salts/hydrates,
   coordination, product plausibility and measurement meaning. RDKit validity
   and worker agreement alone do not establish chemical correctness.
6. `validate_extraction` checks the assembled dataset and readiness. Changing
   claims after chemistry review requires fresh reviews. `export_workbook`
   writes a new revision and re-imports the XLSX through the schema converter.
   A scientific mismatch blocks export and retains diagnostic files.

For manual tool calls, use the schemas advertised by the MCP server. JSON-pointer
paths such as `/conditions/mechanochemistry/frequency` refer to protobuf fields,
not invented spreadsheet headers. Submit a complete value-and-unit object where
appropriate. Source locators are extracted line/page IDs or original PDF bounding
boxes, rather than publisher line numbers.

## 6. Review and share the output

Exports are stored under `runs/<run_id>/exports/<revision>/` and are also exposed
as MCP resources. The export response identifies the actual artifacts.

| Artifact | Purpose |
| --- | --- |
| `extraction.xlsx` | Editable ReactionData plus Overview, Review, Evidence, Sources, Schema and cited PDF figures. |
| `extraction-audit.json` / `.json.gz` | Complete claim, evidence, worker, lease-instruction and decision history. |
| `dataset.json` | Assembled CMCCDB data. |
| `validation.json` / `chemistry-report.json` | Validation results and chemistry review/blocker information. |
| `receipt.json` | Export identity, hashes, round-trip verification and files needed for transport. |
| `workbook-plan.json` | Workbook layout and content used by the writer. |

Inspect SMILES and figures against the original source, check units and header
groups, and review attributions and exclusions before using the dataset. The
workbook is human-editable; edits to ReactionData require renewed validation and
evidence review. Workbook edits are not automatically imported into the claim
ledger. Existing export revisions and human edits are preserved.

For an incomplete review draft, the manager can explicitly call
`export_workbook` with `allow_partial=true`. That workbook remains a draft;
successful file creation does not make it contribution-ready.

When contributing, include the XLSX and **all receipt-listed auxiliary files**.
Large workbooks may have `transport_parts`; submit each whole-record part with
its full attachment set. Large audits may use a fragment manifest and multiple
`.json.gz` envelopes. Keep every fragment and the manifest: they reconstruct the
complete audit without discarding evidence. The coherent master workbook is
retained even when uploads need parts. Check `transport_ready` and the receipt's
size/count results before submitting. This server does not submit to a database
or commit to GitHub.

## Resume, back up and reproduce a run

Reconnect using the same work root and call `list_runs` and `get_run_status`.
Workers should reuse their recorded identity and model revision; completed tasks
are not submitted again, and expired leases can be reacquired. The manager can
inspect paginated `get_audit_events` for the recorded history.

To back up, stop all processes writing to that work root and copy the entire
directory, including the SQLite state, source copies and exports. Restore the
complete directory and reconnect with its path. A live SQLite main file copied
without its WAL state is not a complete snapshot.

Record the source revision of each repository and the Python environment with
your run documentation:

```bash
git -C cmccdb-data rev-parse HEAD
git -C cmccdb-schema rev-parse HEAD
.venv/bin/python -m pip freeze > runs/environment.txt
```

Preserve model revisions, policy version/fingerprint and source hashes from the
audit as well. A changed protobuf fingerprint blocks resuming that run for
extraction until you explicitly migrate its mappings or create a new run with
the matching schema. Merely upgrading the agent policy does not require migrating
the run: new leases receive the current server policy and record it in the audit.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| `harness/` or `agent_policy.py` is missing | Obtain the harness-containing revision or code bundle from the maintainer. |
| Python cannot import `cmccdb_schema` | Install the cloned schema with the same environment executable used by the MCP host. |
| Poppler is unavailable | Set `--poppler-bin` to the directory containing both tools, or fix the MCP process's PATH. |
| `xlsx_ready=false` | Inspect `xlsx_error`; check typed string schema support, Node path and `NODE_PATH` in the host environment. |
| Node cannot resolve `@oai/artifact-tool` | Provision the compatible artifact runtime and verify module resolution with the configured Node executable. |
| No worker can lease a task | Check dependencies and active leases in `get_run_status`; confirm tasks still need workers. |
| An inspection response is too large | Use `compressed=true` with `inspect_consensus` or `validate_extraction` and decode the complete verified envelope using `cmccdb_extraction.responses.decode_inspection_response`; retain every field. |
| A lease or model-identity check fails | Reacquire expired work and use the worker's recorded model revision, rather than changing its identity binding. |
| A field or enum is unknown | Use `schema_catalog`; obtain the matching schema if the source needs a missing field. |
| Chemistry blocks normal export | Inspect `get_chemistry_report`, repair supported claims and perform current source-aware reviews. Use a partial draft for genuinely unresolved evidence. |
| Source-search coverage is incomplete | Finish the source inventory and create a new run if the immutable plan must change. |
| XLSX re-import changes a record | Inspect the retained failed-export diagnostics and repair the conversion/source issue before sharing. |

## Optional installation check

The supplied paper replay is a deterministic protocol/Excel fixture. It does not
call LLMs and does not demonstrate autonomous extraction or independent model
agreement. If the matching test PDFs are present, run it from the workspace:

```bash
.venv/bin/python cmccdb-data/harness/examples/replay_carbonates.py \
  --paper-root "$PWD/cmccdb-data/tests/papers" \
  --work-root "$PWD/runs/reference-fixture" \
  --poppler-bin /absolute/path/to/poppler/bin \
  --node /absolute/path/to/runtime/node \
  --export
```

Use the same `NODE_PATH` as the MCP host. The fixture deliberately exports a
partial review draft. For scientific extraction, use real independent model
contexts and the manager workflow above.
