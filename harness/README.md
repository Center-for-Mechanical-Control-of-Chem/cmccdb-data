# CMCCDB paper extraction through MCP

This initial implementation gives a manager LLM and independent worker LLMs a
shared, persistent extraction pipeline. It reads PDF papers and supporting information (PDF, original XLSX cells, and UTF-8 TeX lines),
grounds claims in page/line locations or image crops, validates schema fields and
units, records disagreements and adjudications, and exports a human-editable XLSX
with an auxiliary audit file. It performs no database contributions or GitHub writes.

The models run in MCP hosts. This server supplies tools, prompts and task state;
registering several worker names does not instantiate several models. No vLLM or
provider-specific inference dependency is required in this release.

## Install and launch

Apply the companion `cmccdb-schema-string-cells.patch` to the local schema package
first. It preserves text such as `00123`, `FALSE`, `NO` and numeric map keys using
the existing converter's field types. No proto change or regenerated file is needed.
The harness uses the currently installed CMCCDB descriptors, validators and unit
resolver, including the screw-speed and feed-rate fields from the patched schema.

Required dependencies:

- Python 3.11+, `mcp>=2.2,<3`, Pydantic 2, `openpyxl>=3.1,<4` for read-only SI indexing, and the local `cmccdb-schema` dependencies.
- Poppler `pdftotext` and `pdftoppm` on PATH or supplied with `--poppler-bin`.
- For XLSX export, Node and a separately provisioned `@oai/artifact-tool` package.
  Set `NODE_PATH` to its containing `node_modules` directory. The tested backend
  comes from the bundled Codex artifact runtime; the harness does not download it
  or assume it is available on a Linux server. PDF indexing, extraction, consensus
  and validation work without this backend. `get_capabilities` checks it before
  expensive model work. Another writer can implement `workbook-plan.json` later,
  provided it passes the mandatory CMCCDB re-import check.

The source-based launcher avoids an editable install in the data repository. From
the Development directory on this Mac, after applying the patches:

```bash
PYTHONDONTWRITEBYTECODE=1 /usr/bin/arch -arm64 \
  .codex/envs/python3.11-codex/bin/python -m pip install \
  -r cmccdb-data/harness/requirements-mcp.txt

export NODE_PATH=/Users/Mark/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules
bash cmccdb-data/harness/launch-mcp.sh \
  --paper-root /Users/Mark/Documents/Postdoc/Development/cmccdb-data/tests/papers \
  --work-root /Users/Mark/Documents/Postdoc/Development/chatgpt_drafts/extraction-runs \
  --poppler-bin /Users/Mark/.cache/codex-runtimes/codex-primary-runtime/dependencies/native/poppler/poppler/bin \
  --node /Users/Mark/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node
```

The launcher forces native ARM execution on macOS. On Linux, set `CMCCDB_PYTHON`
to the configured Python environment and supply that machine's paths. Multiple
stdio processes may share the same **local-filesystem** work root. SQLite WAL is
not intended for sharing this state across hosts over NFS. Use one server host for
the manager and workers; a remote multi-host transport remains future work.
The supplementary requirements supply Protobuf and Werkzeug, which the schema's
validation helpers import and which are absent from this Mac's base requirements.
They keep the existing NumPy, pandas, RDKit and other scientific packages in place.

A generic MCP host configuration for the source-based Mac launcher is:

```json
{
  "mcpServers": {
    "cmccdb-extraction": {
      "command": "/bin/bash",
      "args": [
        "/Users/Mark/Documents/Postdoc/Development/cmccdb-data/harness/launch-mcp.sh",
        "--paper-root", "/Users/Mark/Documents/Postdoc/Development/cmccdb-data/tests/papers",
        "--work-root", "/Users/Mark/Documents/Postdoc/Development/chatgpt_drafts/extraction-runs",
        "--poppler-bin", "/Users/Mark/.cache/codex-runtimes/codex-primary-runtime/dependencies/native/poppler/poppler/bin",
        "--node", "/Users/Mark/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
      ],
      "env": {
        "NODE_PATH": "/Users/Mark/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules"
      }
    }
  }
}
```

`pyproject.toml` also provides `cmccdb-extract-mcp` for a normal package install in
a separately managed environment. MCP SDK v2's `MCPServer` API is used; this is
not a FastMCP v1 server. The protocol was tested with the v2.2 Python stdio client.
See the [official SDK documentation](https://py.sdk.modelcontextprotocol.io/v2/)
for host compatibility and transport details.

## Manager and worker flow

Every MCP initialization, manager prompt, worker prompt and task lease includes
the shared chemistry policy from `src/cmccdb_extraction/agent_policy.py`. It
requires source-supported SMILES, source-aware product chemistry review and
defensible inorganic representations with reported crystal information, preferring
verified CSD/CCDC references. Undefined mixtures and periodic materials require
explicit representation limits rather than invented molecular structures.
Task-specific instructions cannot waive this policy. Scope or schema limitations
must be reported to the manager.

`get_capabilities().agent_policy` returns the policy version and SHA256 of its
complete common and role-specific text. New run manifests record the starting
policy; leases always supply the currently running server's policy, including
when resuming an older run. Each successful lease records that fingerprint and
the exact delivered worker instructions in the audit. After a policy update,
restart the MCP server processes and reconnect all manager/worker sessions;
already running processes keep their loaded instructions. Hosts must pass the
provided instructions to their independent model contexts. Policy delivery does
not establish model compliance or replace the enforced chemistry/export checks.

1. Manager calls `get_capabilities`, `list_papers`, then `create_run` with the main
   paper, all relevant SI files and an explicit curator identity. Sources are
   archived with SHA256 hashes. Paper authors are not automatically made curators
   or experimenters. `idempotency_key` makes retried ingestion recoverable.
2. Manager loads `extraction_manager`, inventories all source pages, searches
   text, and requests actual pixels for tables, figures and spectra. Line numbers
   are stable **extraction IDs**, not publisher line numbers. Coordinates refer to
   the original PDF page in points, with a top-left origin. A PDF text index with
   replacement glyphs includes a warning and requires visual checking of those
   glyphs; a page with no text is marked for visual reading. There is no automatic OCR.
   XLSX SI uses original worksheet names, row numbers and cell addresses; original
   formula text and existing cached results remain distinct and are never executed
   or recalculated. Charts and embedded images require separate visual review.
   TeX SI uses original UTF-8 line numbers and is never compiled. ZIP members must
   be safely staged into a configured root first, with archive/member hashes kept.
3. Manager discovers canonical fields with paginated `schema_catalog`, then
   installs a `Plan`: experiment inventory, permitted field prefixes, source
   locations, dependencies and at least two workers per task. Use semantic tasks
   such as reagent identity/amounts, reactor/conditions, outcomes/analysis,
   workup, and attribution. Each packet must carry the relevant method, captions
   and footnotes, not an isolated number. The plan is immutable after installation;
   create a new run when revising the experiment inventory.
4. The primary LLM delegates each task to separate model contexts in its host.
   Each worker loads `extraction_worker`, leases work with a persistent worker ID
   and actual model revision, and gets evidence plus schema metadata without
   other workers' candidate answers. Vision workers call `view_page` to read
   image regions. There are no numerical claims inferred from caption-only text.
5. Workers submit structured `Submission` objects. Claims use RFC 6901 paths such
   as `/inputs/alcohol/components/0/amount` and
   `/conditions/mechanochemistry/frequency`. Submit a complete `{value, units}`
   object together so equivalent reported units can agree. Required fields include
   `experiment_key`, `path`, `value`, `basis`, `source_value` and `evidence`.
   Text references contain an exact quote; image references contain a page ID,
   original-page bounding box and a written observation. Calculated/inferred
   claims require an explanation and may cite `derived_from` claim IDs.
6. Server rejects unknown fields/enums, conflicting oneof values, incompatible
   units, nonfinite numbers, stale leases and nonexistent quotations atomically.
   Missing information is an abstention or an explicit `unreported` claim, never
   an invented zero. Workers cannot override run IDs or creation provenance.
7. Manager inspects consensus. Matching normalized values from the required
   completed worker identities can agree; conflicting values remain separate.
   Calculations and inference require adjudication even when workers agree.
   `adjudicate_claim` selects an existing candidate or explicitly withholds it;
   actor, reason and previous decisions remain in the audit. A reviewed missing
   optional field need not prevent a valid export. Missing required scientific
   fields still fail the established validators. Parent/child field claims that
   would overwrite each other are flagged.
8. Manager validates coverage and calls `validate_extraction`. It must account
   for all experiments and excluded material before declaring
   `source_search_complete=true` in the plan. Final export requires valid data,
   completed tasks, resolved disagreements, a curator identity and complete
   declared coverage. A fixture run cannot become contribution-ready.
9. `export_workbook` writes a fresh revision, rereads it through the established
   `DatasetConstructor.enumerate_spreadsheet` pipeline, and compares full reaction
   protobufs by stable IDs. Any scientific change withholds the workbook and
   preserves the failed revision under `failed-exports/` for diagnosis. Successful
   files are accessible through MCP resources. The existing converter strips text
   boundary whitespace: export declares every such change in the audit and
   `text-boundary-normalizations.json`, retaining the original worker text. It
   never changes internal text, quantities or array positions.
   Explicit `allow_partial=true` exports a labeled review draft. Existing exports
   and human edits are not overwritten.

Worker identity and model version are recorded assertions by the host. They
cannot prove model independence or chemical correctness. Exact citation checks
prove a quote exists, not that the claim is supported semantically. Source-aware
adjudication and coverage review remain necessary.

## Tools, state and exports

The server exposes 20 tools, two prompts and a resource template. All write tools
modify the configured work root only; there is no shell, arbitrary URL fetching,
database credential or contribution tool. Source documents are untrusted data
and are never executed. Only configured source roots can be ingested. The main
paper must be PDF; SI may also be XLSX or UTF-8 TeX. Non-PDF locators have no
fictitious PDF bounding box, and `view_page` explicitly rejects those locations.

State includes `harness.sqlite3`, immutable source copies/indices, page views,
plans, leases, submissions and append-only decision events. Expired leases can be
reacquired; completed submissions are immutable and exact retries are idempotent.
Stable dataset/reaction IDs survive restarts. A changed protobuf fingerprint
blocks resumed extraction until an explicit mapping migration or a new run.
For backup, stop writers and copy the whole work root, including sources; do not
copy only a live SQLite main file while leaving WAL state behind.

Each successful export contains:

- `extraction.xlsx`: Overview, editable ReactionData with grouped hierarchical
  headers, Review, Evidence, Sources, Schema, and cited image crops in Figures.
  Evidence includes reported/normalized values, exact quotes or observations,
  model revisions and decisions. Cell notes connect accepted fields to claim IDs.
- `extraction-audit.json`: complete candidate, lease-metadata and decision history,
  excluding lease tokens, plus its deterministic lossless `extraction-audit.json.gz`.
  Each reaction stores a compact run/experiment reference; the full gzip is bound
  once per contributed dataset, preventing audit multiplication on URL resolution.
- `dataset.json`, `validation.json`, `receipt.json`, and the workbook layout plan.

For a contribution, attach `extraction-audit.json.gz` alongside the XLSX. The
compressed bytes retain the complete plain JSON, with both hashes in the receipt.
Final export checks the interface's current 5 MiB per-file limit. If the XLSX is
larger, it preserves the coherent full-paper workbook and
returns `transport_parts`: disjoint whole-record XLSX files, each separately
re-imported and carrying the identical full audit snapshot. Submit all parts; the
receipt checks that their record IDs cover the master exactly once. The master
can have `upload_size_checks_passed=false` while `transport_ready=true`. A single
record exceeding the limit remains an explicit failure; no evidence is removed
to make it fit. Oversized complete audit JSON is losslessly partitioned into
upload-sized valid JSON.gz envelopes holding fragments of one complete gzip
stream, plus a JSON.gz manifest. Compressing once keeps repeated audit evidence
compact; independently compressing base64 raw JSON would expand it. The
receipt's `audit_transport_files` and `auxiliary_files` list the actual files to
attach; the oversized original gzip remains archived and is excluded from that
upload list. Every whole-record XLSX part carries every complete audit fragment,
the manifest and every other snapshotted auxiliary attachment. Each file is
SHA256-bound and checked against the 5 MiB limit. Each workbook plus its complete
attachments is also checked against a 30 MiB request budget, reserving 1 MiB
below the interface's 31 MiB limit for multipart metadata. If the shared audit
and auxiliaries alone exceed that budget, export fails before workbook authoring.
If the workbook makes the request too large, whole-record splitting also checks
that aggregate budget in every part.
The complete audit fragments and all other auxiliaries also share a 64-file
attachment cap, matching HTTP preparation, protobuf attachment handling, batch
compilation and the browser upload selector. Exceeding it fails before workbook
authoring; splitting cannot reduce the shared provenance file count.

The manifest records `cmccdb-lossless-json-partition-v2`, ordered compressed-stream
offsets, fragment hashes, the complete gzip SHA256 and the exact original JSON
byte count and SHA256. Reconstruct the original JSON with
`cmccdb_extraction.auxiliary_partition.reassemble_json_parts(manifest_path,
expected_sha256=receipt['audit_json_sha256'])`. Save those exact bytes as
`extraction-audit.json`. To retain the exact archival gzip bytes as well, use
`reassemble_gzip_parts(manifest_path)` and save them under the logical provenance
attachment name `extraction-audit.json.gz`. The decoder also supports existing v1
manifests; those retain exact JSON bytes but do not carry the original gzip stream.
Version, compressed content and size limit appear in immutable filenames so
fresh v2 files never replace earlier fragments. Duplicate keys,
whitespace, number spelling and UTF-8 bytes survive without reserialization.
Overview, Review and Schema include the same reconstruction instructions. A
completed export resource allows only receipt-listed auxiliary filenames and
the fixed export artifacts, confined to that revision; incomplete revisions,
traversal and symlink escapes are rejected.
The XLSX contains cited crops, while the archived originals remain in the run.
Inspect sheet previews and verify exported image anchors against the cited source
pixels. Preview image rendering depends on the separately provisioned backend.
Edited ReactionData needs a fresh validation and reconsidered evidence: there is
no automatic import of human workbook edits into the claim ledger yet.

Canonical quantities use seconds, grams and hertz for time, mass and frequency;
other quantity classes use their first nonzero schema unit. Reported values and
units remain in the audit. Percentages use 50 for 50%. The run's UTC creation
timestamp uses the ISO 8601 compact `+0000` offset to avoid the tested writer's
automatic conversion to an Excel date number. Other date-like labels remain
subject to the re-import guard; a coercion blocks export instead of silently
changing a record.

## Tests and supplied-paper example

From Development, after applying the companion schema patch:

```bash
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$PWD/cmccdb-data/harness/src:$PWD/cmccdb-schema"
export CMCCDB_TEST_PAPERS="$PWD/cmccdb-data/tests/papers"
export CMCCDB_TEST_WORK_ROOT="$PWD/chatgpt_drafts/extraction-test-runs"
export CMCCDB_POPPLER_BIN=/Users/Mark/.cache/codex-runtimes/codex-primary-runtime/dependencies/native/poppler/poppler/bin
export CMCCDB_XLSX_NODE=/Users/Mark/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node
export NODE_PATH=/Users/Mark/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules
mkdir -p "$CMCCDB_TEST_WORK_ROOT"
/usr/bin/arch -arm64 .codex/envs/python3.11-codex/bin/python \
  -m unittest discover -s cmccdb-data/harness/tests -v

/usr/bin/arch -arm64 .codex/envs/python3.11-codex/bin/python \
  cmccdb-data/harness/examples/replay_carbonates.py \
  --paper-root "$CMCCDB_TEST_PAPERS" --work-root "$CMCCDB_TEST_WORK_ROOT/reference" \
  --poppler-bin "$CMCCDB_POPPLER_BIN" --node "$CMCCDB_XLSX_NODE" --export
```

The tests index every supplied PDF; they currently cover eight papers plus two
SI files. They exercise actual stdio MCP clients, shared state across separate
server processes, restart recovery, image responses, schema/quote/unit/lease
checks, disagreements, preserved failed exports and actual Excel re-imports at one,
three and 120 records with multiple header shapes, repeated scalars, byte data,
numeric text, zero and `False`. XLSX tests skip explicitly if the writer is absent.

`replay_carbonates.py` pins the hashes of `d6mr00048g.pdf` and its actual SI,
replays manually selected reference claims through MCP, and exports Compound 3
Methods C.1–C.3. Reported yields are 84%, 22% and 78%. The workbook includes a
Table 1 crop and a cited SI spectrum crop, without invented peak assignments.
Roles and the inferred weight-analysis classification are explicitly reviewed.
This is an incomplete-paper **test fixture**, not a claim of autonomous extraction
or an independent two-model scientific benchmark. Live model evaluation requires
connecting actual separate worker contexts and recording their model revisions.


## Structure extraction and chemistry approval

Every input component, molecular workup component and product retains its original
NAME identifiers and receives source-supported isomeric SMILES/CXSMILES when its
molecular graph is resolvable. Workers must read actual structural figures and
applicable labels; they must preserve charge, salt/counterions, hydrate components,
stereochemistry and coordination. A graph transcribed from a diagram or resolved
from an unambiguous source identity needs an explained basis and exact evidence;
RDKit parsing alone does not establish the source identity or chemical plausibility.
Optional `Submission.structure_assessments` records each worker's cited status and
reason without pretending that matching labels prove independent chemical approval.

The server uses real RDKit parsing and sanitization, reports formula, total charge,
fragments, assigned/unassigned stereocenters and stereo bonds, rejects wildcard
or contradictory graphs and compares any explicitly source-grounded expected
formula/charge/stereo requirements. It does not automatically choose a structure,
strip salts, add missing stereochemistry or generate molecules from names.

`get_chemistry_report(run_id)` returns exact dataset/reaction/claim SHA256 snapshots,
all original compound paths/names, parsed structure results, selected identity and
crystal claim IDs, coverage and blockers. After independently checking source
chemistry, the manager calls `review_reaction_chemistry(run_id, review)` with the
strict `ReactionChemistryReview` contract advertised by the MCP tool. Each current
reaction needs named reviewer, rationale, exact hashes, source evidence, approved
reaction-plausibility and measurement-semantics checks, and exactly one review for
every current compound. Compound checks cover identity, connectivity, formula,
charge, stereochemistry, salt/hydrate and coordination. `not_applicable` must be
explicitly justified; source identity and molecular connectivity/formula/charge
must actually be approved. Source-grounded expected formula/charge and stereo
counts are optional additional checks, not invented source facts.

Compound statuses are `resolved_molecular`, `periodic_crystal`,
`nonmolecular_material`, `mixture` or `unresolved`. Molecular resolution requires
a valid, source-supported SMILES. The narrow `nonmolecular_material` exception
supports source-identified liquid elemental mercury: `[Hg]` is an explicitly
limited `elemental_composition` token with reviewed formula and charge, never a
discrete molecule or crystal. Crystal fields must be absent and the crystal
reference status must be `not_applicable` for this exception.
Periodic and true-mixture exceptions require explicit reasons, resolved identity
and composition, source evidence and manager approval; they cannot assert made-up
finite SMILES. Unknown chemical graphs remain `unresolved` and always block normal
export, even if the schema itself accepts a NAME. For inorganic crystalline solids
prefer actual source-reported CSD identity and crystal parameters where available.
Record `crystal_reference_status=source_not_reported` if the source lacks them;
never invent a CSD number, CIF identity or cell. A crystal reference also needs the
manager's actual source evidence and current selected crystal claim IDs.

A periodic solid may also retain a source-supported composition token with explicit
`structure_scope=stoichiometric_formula_unit` or `elemental_composition`, a nonempty
`representation_limit` and source-reviewed `expected_formula`. Such a token never
claims actual lattice bonds, free ions or measured oxidation states. Arbitrary atom
bags, guessed mixed-valence assignments and nonstoichiometric supercells remain
unsupported. A molecular graph has `structure_scope=molecular_graph`; a material
without a unique finite graph uses `no_unique_finite_graph` and an explicit status.

For many explicit choices, `adjudicate_claims(run_id, decisions)` selects 1–500
existing candidate IDs atomically. Each item supplies exact experiment/path,
claim ID, actor and reason. Any invalid item rolls back every decision and audit
event in that batch. Each field occurs once; use `adjudicate_claim` for withholding.

`get_run_status(run_id)` exposes each task's sorted `completed_workers` IDs for
reliable resume after a restart. It includes only completed assignments belonging
to that run and task; lease tokens and submission payloads are not exposed.

For large inventories, `inspect_consensus(run_id, compressed=True)` and
`validate_extraction(run_id, compressed=True)` return the same complete payload
as canonical UTF-8 JSON inside a `cmccdb-gzip-json-v1` envelope: `gzip_base64`,
`uncompressed_bytes`, `json_sha256` and `gzip_sha256`. Hosts must verify both
SHA256s and the decoded byte length; the local `decode_inspection_response`
helper provides bounded decoding. Compressed gzip bytes are capped at 16 MiB;
exceeding the cap fails clearly and never truncates claims, evidence or validation.
Default calls keep their existing uncompressed responses, and no tools are added.

Large evidence workbooks use a bounded Node child-process budget: the default
V8 heap is 8192 MiB and the export timeout is 600 seconds. Configure
`CMCCDB_XLSX_HEAP_MIB` within 512–8192 and `CMCCDB_XLSX_TIMEOUT_SECONDS` within
30–1800 for the host. The largest full-paper audits require sufficient additional
host memory for Python and native XLSX buffers as well as that heap. The backend
uses shared text styles, restores numeric/Boolean formatting explicitly and
writes values in 256-row chunks. All evidence, notes, blank-column positions and
whole scientific records remain intact, and exact protobuf re-import is mandatory.
No formulas are authored. The backend recalculates once before final rendering,
inspection and export, following the spreadsheet verification workflow. Backend
stage/memory diagnostics accompany each export without changing scientific data.

The companion draft schema adds optional `ProductCompound.crystal_parameters=100`,
matching the existing `Compound` field without renumbering, and adds
`Length.ANGSTROM=6` with Å/angstrom aliases and exact conversion support. Cell
lengths normalize to Å while reactor/ball lengths keep existing centimeter behavior.
All generated Python/typing/JavaScript/schema-JSON files come from the established
`compile_proto_wrappers.sh` pipeline, followed by its existing patch/documentation
helpers. Existing crystal validators only check type/details and nonempty database
identifier; the chemistry layer adds positive cell lengths and physically bounded
angles, while evidence review remains responsible for source fidelity.

Normal export additionally requires `validation.chemistry_ready=true` for every
reaction. `validation.chemistry_blockers` and `chemistry_report` remain available
on incomplete datasets. `allow_partial=true` exports a clearly labeled draft and
cannot bypass missing chemistry approval for contribution readiness. Stored
reviews and append-only history are retained in the full extraction audit, with a
compact per-reaction `provenance.reaction_metadata.chemistry_review` reference.
Any changed dataset content, selected claim or current candidate inventory makes
an approval stale; rerun source-aware review after edits. Old extraction runs have
no implicit grandfathered chemistry approval. A changed schema fingerprint requires
a fresh run or explicit migration, preserving the original scientific claims/audit.
