"""Versioned, strict contracts shared by the MCP manager and workers."""

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EvidenceRef(Contract):
    evidence_id: str
    quote: str = ""
    # PDF points, top-left origin, in the original page's coordinate system.
    bbox: list[float] | None = None
    observation: str = ""


class Claim(Contract):
    experiment_key: str = Field(min_length=1, max_length=160)
    path: str = Field(min_length=2, max_length=1024)
    value: Any
    evidence: list[EvidenceRef] = Field(min_length=1, max_length=30)
    basis: Literal["reported", "calculated", "inferred", "unreported"] = "reported"
    source_value: str = Field(min_length=1, max_length=2000)
    explanation: str = Field(default="", max_length=4000)
    derived_from: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("path")
    @classmethod
    def pointer(cls, value):
        if not value.startswith("/") or "//" in value:
            raise ValueError("path must be a nonempty RFC 6901 JSON pointer")
        return value


class Experiment(Contract):
    key: str = Field(min_length=1, max_length=160)
    label: str = Field(min_length=1, max_length=500)
    evidence_ids: list[str] = Field(min_length=1, max_length=100)
    scope: str = Field(min_length=1, max_length=4000)


class Task(Contract):
    task_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    experiment_keys: list[str] = Field(min_length=1, max_length=100)
    allowed_paths: list[str] = Field(min_length=1, max_length=100)
    evidence_ids: list[str] = Field(min_length=1, max_length=100)
    instructions: str = Field(min_length=1, max_length=8000)
    modality: Literal["text", "vision", "mixed"] = "text"
    depends_on: list[str] = Field(default_factory=list, max_length=50)
    required_workers: int = Field(default=2, ge=2, le=5)


class Plan(Contract):
    experiments: list[Experiment] = Field(min_length=1, max_length=1000)
    tasks: list[Task] = Field(min_length=1, max_length=1000)
    # Explicitly account for tables/figures/controls not extracted in this revision.
    coverage_notes: list[str] = Field(default_factory=list, max_length=1000)
    source_search_complete: bool = False


class Submission(Contract):
    claims: list[Claim] = Field(default_factory=list, max_length=500)
    abstentions: list[str] = Field(default_factory=list, max_length=100)
    structure_assessments: list["StructureAssessment"] = Field(default_factory=list, max_length=1000)



class ChemistryCheck(Contract):
    status: Literal["approved", "not_applicable", "needs_revision"]
    reason: str = Field(min_length=1, max_length=4000)


class ClaimAdjudication(Contract):
    experiment_key: str = Field(min_length=1, max_length=160)
    path: str = Field(min_length=1, max_length=1024)
    claim_id: str = Field(min_length=1, max_length=160)
    actor: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=4000)


class AdjudicationBatch(Contract):
    decisions: list[ClaimAdjudication] = Field(min_length=1, max_length=500)


class CompoundChemistryChecks(Contract):
    identity: ChemistryCheck
    connectivity: ChemistryCheck
    formula: ChemistryCheck
    charge: ChemistryCheck
    stereochemistry: ChemistryCheck
    salt_hydrate: ChemistryCheck
    coordination: ChemistryCheck


class StructureAssessment(Contract):
    experiment_key: str = Field(min_length=1, max_length=160)
    compound_path: str = Field(min_length=2, max_length=1024)
    status: Literal["resolved_molecular", "periodic_crystal", "nonmolecular_material", "mixture", "unresolved"]
    reason: str = Field(min_length=1, max_length=4000)
    evidence: list[EvidenceRef] = Field(min_length=1, max_length=30)


class CompoundChemistryReview(Contract):
    compound_path: str = Field(min_length=2, max_length=1024)
    status: Literal["resolved_molecular", "periodic_crystal", "nonmolecular_material", "mixture", "unresolved"]
    reason: str = Field(min_length=1, max_length=4000)
    evidence: list[EvidenceRef] = Field(min_length=1, max_length=30)
    claim_ids: list[str] = Field(min_length=1, max_length=500)
    checks: CompoundChemistryChecks
    # Only populate from a cited source; these are checks, never invented identifiers.
    expected_formula: str = Field(default="", max_length=500)
    expected_formal_charge: int | None = None
    required_stereo_centers: int = Field(default=0, ge=0, le=500)
    required_stereo_bonds: int = Field(default=0, ge=0, le=500)
    crystal_reference_status: Literal["present", "source_not_reported", "not_applicable"]
    exception_components_resolved: bool = False
    structure_scope: Literal["molecular_graph", "stoichiometric_formula_unit", "elemental_composition", "no_unique_finite_graph"] = "molecular_graph"
    representation_limit: str = Field(default="", max_length=4000)


class ReactionChemistryReview(Contract):
    experiment_key: str = Field(min_length=1, max_length=160)
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reaction_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    claims_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    actor: str = Field(min_length=1, max_length=500)
    disposition: Literal["approved", "needs_revision"]
    rationale: str = Field(min_length=1, max_length=8000)
    evidence: list[EvidenceRef] = Field(min_length=1, max_length=100)
    reaction_plausibility: ChemistryCheck
    measurement_semantics: ChemistryCheck
    compounds: list[CompoundChemistryReview] = Field(min_length=1, max_length=1000)

Submission.model_rebuild()
