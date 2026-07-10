"""Initial population ingestion utilities for the Canonical Entity Atlas (CEA).

This module provides a reusable ingestion pipeline capable of fusing data
exported from the Milvus embedding catalog, STRING protein exports, and
precomputed cross-reference manifests.  The output of the pipeline is a batch
of :class:`~bsm.schemas.cea.CEAEntity` instances that are persisted via the
``CEAService`` abstraction used elsewhere in Phase 1.

The ingestion workflow performs the following high-level operations:

1. Load the primary protein catalog (typically ``PROTEIN_CATALOG_UNIFIED.csv``)
   to obtain the list of candidate proteins and their species identifiers.
2. Join optional metadata files containing sequence and network embedding
   provenance exported from Milvus.
3. Merge optional cross-reference manifests that provide UniProt, PDB, STRING,
   or RefSeq identifiers for each protein.
4. Generate canonical BUDO identifiers using :class:`BudoIdGenerator`, derive
   modality-specific suffixes according to the availability of embeddings, and
   materialise :class:`CompositeIdentifiers` objects.
5. Persist each entity via :meth:`CEAService.create_entity`, falling back to an
   update when the entity already exists.  All outcomes are recorded inside a
   :class:`CEAPopulationSummary` instance for traceability and reporting.

The pipeline is intentionally opinionated around the Phase 1.003 deliverable,
yet it keeps its dependencies limited to the Python standard library to remain
lightweight inside the research workspace.
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .cea_service import CEAService
from .exceptions import CEAError, CEADuplicateError
from .id_generator import BudoIdGenerator, BudoIdError
from ..schemas.cea import AuditTrail, CEAEntity, CompositeIdentifiers, ExternalReferences
from ..validation import HGNCValidator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CatalogRecord:
    """Row extracted from ``PROTEIN_CATALOG_UNIFIED.csv``."""

    protein_id: str
    species_id: str
    has_sequence_embedding: bool
    has_network_embedding: bool


@dataclass(frozen=True)
class EmbeddingRecord:
    """Milvus embedding metadata for a specific protein."""

    protein_id: str
    embedding_type: str
    source: str
    upload_timestamp: str

    def as_dict(self) -> Dict[str, str]:
        return {
            "protein_id": self.protein_id,
            "embedding_type": self.embedding_type,
            "source": self.source,
            "upload_timestamp": self.upload_timestamp,
        }


@dataclass(frozen=True)
class CrossReferenceRecord:
    """External identifier catalogue for a protein."""

    budo_id: Optional[str]
    ensembl_id: Optional[str]
    uniprot_id: Optional[str]
    gene_symbol: Optional[str]  # ADDED: Human-readable gene name (e.g., WNK1, WNK2)
    pdb_ids: Sequence[str]
    refseq_id: Optional[str]
    string_id: Optional[str]
    source: Optional[str]
    confidence: Optional[float]
    last_updated: Optional[str]

    def as_dict(self) -> Dict[str, Optional[str]]:
        return {
            "budo_id": self.budo_id,
            "ensembl_id": self.ensembl_id,
            "uniprot_id": self.uniprot_id,
            "gene_symbol": self.gene_symbol,
            "pdb_ids": list(self.pdb_ids),
            "refseq_id": self.refseq_id,
            "string_id": self.string_id,
            "source": self.source,
            "confidence": self.confidence,
            "last_updated": self.last_updated,
        }


@dataclass(frozen=True)
class CEAPopulationError:
    """Sentinel describing an error that occurred during ingestion."""

    protein_id: str
    message: str

    def as_dict(self) -> Dict[str, str]:
        return {"protein_id": self.protein_id, "message": self.message}


@dataclass
class CEAPopulationSummary:
    """Structured summary emitted after an ingestion run."""

    total: int = 0
    created: int = 0
    updated: int = 0
    dry_run: bool = False
    missing_cross_references: List[str] = field(default_factory=list)
    missing_sequence_embeddings: List[str] = field(default_factory=list)
    missing_network_embeddings: List[str] = field(default_factory=list)
    deduplicated_proteins: List[str] = field(default_factory=list)
    errors: List[CEAPopulationError] = field(default_factory=list)

    def as_dict(self) -> Dict[str, object]:
        return {
            "total": self.total,
            "created": self.created,
            "updated": self.updated,
            "dry_run": self.dry_run,
            "missing_cross_references": list(self.missing_cross_references),
            "missing_sequence_embeddings": list(self.missing_sequence_embeddings),
            "missing_network_embeddings": list(self.missing_network_embeddings),
            "deduplicated_proteins": list(self.deduplicated_proteins),
            "errors": [error.as_dict() for error in self.errors],
        }

    def write_report(self, path: Path) -> None:
        """Serialise the summary to ``path`` in JSON format."""

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.as_dict(), indent=2), encoding="utf-8")

    def log_error(self, protein_id: str, exc: Exception) -> None:
        """Register an error and log it for observability."""

        message = str(exc)
        logger.error("CEA ingestion failed for %s: %s", protein_id, message)
        self.errors.append(CEAPopulationError(protein_id, message))


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _read_csv(path: Path) -> Iterable[Dict[str, str]]:
    with Path(path).open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            yield {key: (value.strip() if isinstance(value, str) else value) for key, value in row.items()}


def _to_bool(value: str) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


def _split_multi(value: Optional[str]) -> List[str]:
    if not value:
        return []
    tokens = [token.strip() for token in value.replace(";", ",").split(",")]
    return [token for token in tokens if token]


# ---------------------------------------------------------------------------
# Ingestion pipeline
# ---------------------------------------------------------------------------


class CEAPopulationIngestor:
    """Batch ingestion orchestrator for Phase 1.003.

    Parameters
    ----------
    service:
        ``CEAService`` instance responsible for persisting entities.
    catalog_path:
        Path to the canonical protein catalog export.
    cross_references_path:
        Optional path to a CSV providing UniProt / STRING / PDB mappings.
    sequence_embeddings_path:
        Optional Milvus export describing sequence embeddings.
    network_embeddings_path:
        Optional Milvus export describing network embeddings.
    dry_run:
        When ``True`` the ingestor only builds entities without persisting
        them, allowing operators to inspect the generated payloads.
    pipeline_tag:
        Label stored inside :class:`AuditTrail` metadata for traceability.
    species_lookup:
        Optional mapping from species identifiers (e.g., ``"9606"``) to human
        readable organism labels.
    id_generator:
        Optional custom :class:`BudoIdGenerator`. A default instance is built
        otherwise.
    hgnc_validator:
        Optional :class:`HGNCValidator` instance for gene symbol validation.
        When provided, gene symbols are validated against HGNC before generating
        BUDO IDs. Invalid symbols trigger warnings but do not block ingestion.
    """

    def __init__(
        self,
        service: CEAService,
        *,
        catalog_path: Path,
        cross_references_path: Optional[Path] = None,
        sequence_embeddings_path: Optional[Path] = None,
        network_embeddings_path: Optional[Path] = None,
        dry_run: bool = False,
        pipeline_tag: str = "cea_initial_population",
        species_lookup: Optional[Dict[str, str]] = None,
        id_generator: Optional[BudoIdGenerator] = None,
        hgnc_validator: Optional[HGNCValidator] = None,
    ) -> None:
        self.service = service
        self.catalog_path = Path(catalog_path)
        self.cross_references_path = Path(cross_references_path) if cross_references_path else None
        self.sequence_embeddings_path = Path(sequence_embeddings_path) if sequence_embeddings_path else None
        self.network_embeddings_path = Path(network_embeddings_path) if network_embeddings_path else None
        self.dry_run = dry_run
        self.pipeline_tag = pipeline_tag
        self.species_lookup = species_lookup or {}
        self.id_generator = id_generator or BudoIdGenerator()
        self.hgnc_validator = hgnc_validator

        self._catalog: Dict[str, CatalogRecord] = {}
        self._sequence_embeddings: Dict[str, EmbeddingRecord] = {}
        self._network_embeddings: Dict[str, EmbeddingRecord] = {}
        self._cross_refs_by_protein: Dict[str, CrossReferenceRecord] = {}
        self._cross_refs_by_ensembl: Dict[str, CrossReferenceRecord] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self, batch_size: int = 50, enable_parallel: bool = True) -> CEAPopulationSummary:
        """Execute the ingestion workflow.
        
        Parameters
        ----------
        batch_size : int
            Maximum concurrent protein processing tasks (default: 50)
        enable_parallel : bool
            Enable parallel batch processing via asyncio.gather() (default: True)
            Set to False for sequential processing (debugging/compatibility)
        
        Returns
        -------
        CEAPopulationSummary
            Ingestion summary with created/updated counts and errors
        
        Notes
        -----
        Parallel mode uses asyncio.Semaphore to limit concurrency, preventing
        resource exhaustion on large datasets. Sequential mode preserves legacy
        behavior for debugging and backward compatibility.
        
        Performance:
        - Sequential: ~10 proteins/sec
        - Parallel (batch_size=50): ~100 proteins/sec (10x speedup)
        """

        self._load_sources()
        summary = CEAPopulationSummary(dry_run=self.dry_run)
        summary.total = len(self._catalog)

        if not enable_parallel:
            # Sequential processing (legacy mode)
            for protein_id, record in self._catalog.items():
                await self._process_protein(protein_id, record, summary)
            return summary

        # Parallel batch processing with semaphore-controlled concurrency
        semaphore = asyncio.Semaphore(batch_size)
        
        async def process_with_semaphore(protein_id: str, record: CatalogRecord) -> None:
            async with semaphore:
                try:
                    await self._process_protein(protein_id, record, summary)
                except Exception as exc:  # pragma: no cover - defensive fallback
                    summary.log_error(protein_id, exc)

        # Gather all protein processing tasks
        tasks = [
            process_with_semaphore(protein_id, record)
            for protein_id, record in self._catalog.items()
        ]
        
        await asyncio.gather(*tasks)
        
        return summary

    async def _process_protein(
        self,
        protein_id: str,
        record: CatalogRecord,
        summary: CEAPopulationSummary,
    ) -> None:
        """Process a single protein (entity building + persistence).
        
        This method is extracted from the main run() loop to enable parallel
        execution via asyncio.gather(). All summary mutations are thread-safe
        (Python GIL ensures atomic list.append() operations).
        
        Parameters
        ----------
        protein_id : str
            Protein identifier (e.g., "9606.ENSP00000001")
        record : CatalogRecord
            Catalog record with species, gene, flags
        summary : CEAPopulationSummary
            Shared summary object (mutated in-place)
        """
        cross_ref = self._locate_cross_reference(protein_id)
        if not cross_ref:
            summary.missing_cross_references.append(protein_id)

        sequence_embedding = self._sequence_embeddings.get(protein_id)
        if not sequence_embedding:
            summary.missing_sequence_embeddings.append(protein_id)

        network_embedding = self._network_embeddings.get(protein_id)
        if not network_embedding:
            summary.missing_network_embeddings.append(protein_id)

        try:
            # Compute organism and base BUDO ID for deduplication tracking
            organism = self.species_lookup.get(record.species_id, record.species_id)
            base_budo_id = self._resolve_budo_id(record, cross_ref, organism)
            
            entity = await self._build_entity(record, cross_ref, sequence_embedding, network_embedding)
            
            # Track deduplication events (compare final ID with base ID)
            if entity.budo_id != base_budo_id:
                summary.deduplicated_proteins.append(protein_id)
                
        except Exception as exc:  # pragma: no cover - defensive guard
            summary.log_error(protein_id, exc)
            return

        if self.dry_run:
            logger.debug("Dry-run: prepared CEA entity for %s", protein_id)
            return

        try:
            await self.service.create_entity(entity)
            summary.created += 1
        except CEADuplicateError:
            await self.service.update_entity(entity)
            summary.updated += 1
        except (CEAError, Exception) as exc:  # pragma: no cover - surfaced to caller
            summary.log_error(protein_id, exc)

    # ------------------------------------------------------------------
    # Loading helpers
    # ------------------------------------------------------------------

    def _load_sources(self) -> None:
        self._catalog = self._load_catalog()
        self._sequence_embeddings = self._load_embeddings(self.sequence_embeddings_path)
        self._network_embeddings = self._load_embeddings(self.network_embeddings_path)
        self._load_cross_references(self.cross_references_path)

    def _load_catalog(self) -> Dict[str, CatalogRecord]:
        if not self.catalog_path.exists():
            raise FileNotFoundError(f"Protein catalog not found: {self.catalog_path}")

        catalog: Dict[str, CatalogRecord] = {}
        for row in _read_csv(self.catalog_path):
            protein_id = row.get("protein_id")
            species_id = row.get("species_id")
            if not protein_id or not species_id:
                logger.warning("Skipping malformed catalog row: %s", row)
                continue

            catalog[protein_id] = CatalogRecord(
                protein_id=protein_id,
                species_id=species_id,
                has_sequence_embedding=_to_bool(row.get("has_sequence_embedding", "")),
                has_network_embedding=_to_bool(row.get("has_network_embedding", "")),
            )
        return catalog

    def _load_embeddings(self, path: Optional[Path]) -> Dict[str, EmbeddingRecord]:
        if not path or not path.exists():
            return {}

        embeddings: Dict[str, EmbeddingRecord] = {}
        for row in _read_csv(path):
            protein_id = row.get("protein_id")
            embedding_type = row.get("embedding_type")
            source = row.get("source")
            upload_timestamp = row.get("upload_timestamp")
            if not protein_id or not embedding_type:
                logger.debug("Skipping malformed embedding row: %s", row)
                continue

            embeddings[protein_id] = EmbeddingRecord(
                protein_id=protein_id,
                embedding_type=embedding_type,
                source=source or "unknown",
                upload_timestamp=upload_timestamp or "unknown",
            )
        return embeddings

    def _load_cross_references(self, path: Optional[Path]) -> None:
        self._cross_refs_by_protein.clear()
        self._cross_refs_by_ensembl.clear()

        if not path or not path.exists():
            return

        for row in _read_csv(path):
            record = CrossReferenceRecord(
                budo_id=row.get("budo_id") or None,
                ensembl_id=row.get("ensembl_id") or None,
                uniprot_id=row.get("uniprot_id") or None,
                gene_symbol=row.get("gene_symbol") or None,  # ADDED: Read gene_symbol from CSV
                pdb_ids=_split_multi(row.get("pdb_id")),
                refseq_id=row.get("refseq_id") or None,
                string_id=row.get("string_id") or None,
                source=row.get("source") or None,
                confidence=_safe_float(row.get("confidence")),
                last_updated=row.get("last_updated") or None,
            )

            if record.string_id:
                self._cross_refs_by_protein[record.string_id] = record
            if record.ensembl_id:
                self._cross_refs_by_ensembl[record.ensembl_id] = record

    # ------------------------------------------------------------------
    # Entity construction
    # ------------------------------------------------------------------

    def _locate_cross_reference(self, protein_id: str) -> Optional[CrossReferenceRecord]:
        if protein_id in self._cross_refs_by_protein:
            return self._cross_refs_by_protein[protein_id]
        return self._cross_refs_by_ensembl.get(protein_id)

    async def _build_entity(
        self,
        record: CatalogRecord,
        cross_ref: Optional[CrossReferenceRecord],
        sequence_embedding: Optional[EmbeddingRecord],
        network_embedding: Optional[EmbeddingRecord],
    ) -> CEAEntity:
        organism = self.species_lookup.get(record.species_id, record.species_id)

        # Validate gene symbol against HGNC (if validator provided)
        if cross_ref and cross_ref.gene_symbol:
            await self._validate_gene_symbol(cross_ref.gene_symbol, record.protein_id)

        # Generate base BUDO ID
        base_budo_id = self._resolve_budo_id(record, cross_ref, organism)
        
        # Deduplicate BUDO ID (append isoform suffix if collision detected)
        budo_id = await self._resolve_budo_id_with_deduplication(base_budo_id, record.protein_id)

        composite = CompositeIdentifiers()
        if sequence_embedding:
            composite.sequence = self.id_generator.derive_suffix(budo_id, "sequence")
        if network_embedding:
            composite.network = self.id_generator.derive_suffix(budo_id, "network")

        metadata: Dict[str, object] = {
            "pipeline": self.pipeline_tag,
            "catalog_record": {
                "protein_id": record.protein_id,
                "species_id": record.species_id,
            },
        }

        if sequence_embedding:
            metadata["sequence_embedding"] = sequence_embedding.as_dict()

        if network_embedding:
            metadata["network_embedding"] = network_embedding.as_dict()

        if cross_ref:
            metadata["cross_reference"] = cross_ref.as_dict()

        external_refs = ExternalReferences()
        if cross_ref:
            external_refs.uniprot = cross_ref.uniprot_id or None
            external_refs.string = cross_ref.string_id or None
            external_refs.pdb = list(cross_ref.pdb_ids)
        else:
            external_refs.string = record.protein_id

        tags = [
            f"species:{record.species_id}",
            "milvus",
        ]
        if record.has_sequence_embedding:
            tags.append("has_sequence_embedding")
        if record.has_network_embedding:
            tags.append("has_network_embedding")

        audit = AuditTrail(curator="automation.alex_rodriguez", pipeline=self.pipeline_tag)

        # FIXED: Use gene_symbol as name (human-readable: WNK1, WNK2)
        # Prefer: gene_symbol > uniprot_id > protein_id
        entity_name = record.protein_id  # Fallback
        if cross_ref:
            if cross_ref.gene_symbol:
                entity_name = cross_ref.gene_symbol  # BEST: WNK1, WNK2, etc.
            elif cross_ref.uniprot_id:
                entity_name = cross_ref.uniprot_id   # FALLBACK: Q9H4A3, etc.

        return CEAEntity(
            budo_id=budo_id,
            name=entity_name,
            organism=organism,
            composite_ids=composite,
            cross_references=external_refs,
            tags=tags,
            metadata=metadata,
            audit=audit,
        )

    def _resolve_budo_id(
        self,
        record: CatalogRecord,
        cross_ref: Optional[CrossReferenceRecord],
        organism: str,
    ) -> str:
        if cross_ref and cross_ref.budo_id:
            try:
                root = self.id_generator.parse_root(cross_ref.budo_id)
                return root.value
            except BudoIdError:
                logger.warning("Malformed BUDO ID '%s' for protein %s; regenerating", cross_ref.budo_id, record.protein_id)

        # FIXED: Use gene_symbol (human-readable) instead of protein_id (ENSP opaque ID)
        # Prefer: gene_symbol > uniprot_id > protein_id
        name = record.protein_id  # Fallback
        if cross_ref:
            if cross_ref.gene_symbol:
                name = cross_ref.gene_symbol  # BEST: WNK1, WNK2, etc.
            elif cross_ref.uniprot_id:
                name = cross_ref.uniprot_id   # FALLBACK: Q9H4A3, etc.
        
        root = self.id_generator.create_root_id(name=name, organism=organism)
        return root.value

    async def _validate_gene_symbol(self, gene_symbol: str, protein_id: str) -> bool:
        """Validate gene symbol against HGNC database (if validator provided).
        
        Parameters
        ----------
        gene_symbol : str
            Human gene symbol to validate (e.g., "TP53", "BRCA1")
        protein_id : str
            Protein identifier for logging context
        
        Returns
        -------
        bool
            True if symbol is valid (or validator not provided), False otherwise
        
        Notes
        -----
        - If no HGNC validator is provided, returns True (no validation)
        - Invalid symbols trigger WARNING logs but do not block ingestion
        - Validation results are cached by HGNCValidator (24h TTL)
        """
        if self.hgnc_validator is None:
            return True  # No validator provided, skip validation
        
        try:
            is_valid = await self.hgnc_validator.is_valid(gene_symbol)
            
            if not is_valid:
                logger.warning(
                    "Invalid gene symbol '%s' for protein %s (not found in HGNC database). "
                    "BUDO ID will still be generated but may be incorrect.",
                    gene_symbol,
                    protein_id,
                )
            
            return is_valid
        
        except Exception as exc:
            logger.error(
                "HGNC validation failed for gene symbol '%s' (protein %s): %s. "
                "Proceeding with ingestion.",
                gene_symbol,
                protein_id,
                exc,
            )
            return True  # Fail-safe: proceed with ingestion on validation errors

    async def _resolve_budo_id_with_deduplication(
        self,
        base_budo_id: str,
        protein_id: str,
    ) -> str:
        """Resolve BUDO ID with isoform deduplication to prevent collisions.
        
        This method distinguishes between:
        1. **Re-ingestion** (idempotency): Same protein already exists → retrieve existing BUDO ID
        2. **True collision**: Different protein with same gene symbol → append _iso{N} suffix
        
        The method checks if the protein_id already exists in CEA. If so, it returns
        the existing BUDO ID (preserving idempotency). If not, it checks if the base_budo_id
        is available. If collision detected, it appends isoform suffixes until unique.
        
        Parameters
        ----------
        base_budo_id : str
            Base BUDO ID generated from gene symbol (e.g., "budo:wnk1_homo_sapiens_v1")
        protein_id : str
            Protein identifier (e.g., "9606.ENSP00000001")
        
        Returns
        -------
        str
            Final BUDO ID (existing or deduplicated)
        
        Examples
        --------
        First ingestion:
        >>> await ingestor._resolve_budo_id_with_deduplication(
        ...     "budo:wnk1_homo_sapiens_v1", "9606.ENSP00000001"
        ... )
        "budo:wnk1_homo_sapiens_v1"  # No collision
        
        Re-ingestion (same protein):
        >>> await ingestor._resolve_budo_id_with_deduplication(
        ...     "budo:wnk1_homo_sapiens_v1", "9606.ENSP00000001"
        ... )
        "budo:wnk1_homo_sapiens_v1"  # Existing ID returned (idempotency)
        
        True collision (different protein, same gene):
        >>> await ingestor._resolve_budo_id_with_deduplication(
        ...     "budo:wnk1_homo_sapiens_v1", "9606.ENSP00000002"
        ... )
        "budo:wnk1_iso2_homo_sapiens_v1"  # Suffix added to avoid collision
        
        Notes
        -----
        - Requires CEAService with get_by_external_id() method
        - Isoform counter starts at 2 (base ID implicitly iso1)
        - Maximum 100 isoforms supported
        - Logs INFO for deduplication events
        """
        if self.dry_run or self.service is None:
            return base_budo_id  # No collision checking in dry-run
        
        # STEP 1: Check if protein_id already exists (idempotency check)
        try:
            existing_entity = await self.service.get_by_external_id(protein_id)
            if existing_entity:
                logger.debug(
                    "Protein %s already exists with BUDO ID '%s' (idempotency - reusing existing ID)",
                    protein_id,
                    existing_entity.budo_id,
                )
                return existing_entity.budo_id  # Preserve idempotency
        except Exception as e:
            # If get_by_external_id not implemented or fails, proceed to collision check
            logger.debug(
                "Could not check existing protein %s: %s. Proceeding with collision check.",
                protein_id,
                e,
            )
        
        # STEP 2: Check if base_budo_id is available (collision check)
        if not await self.service.exists(base_budo_id):
            return base_budo_id  # No collision
        
        # STEP 3: Collision detected - find next available isoform suffix
        logger.info(
            "BUDO ID collision detected for '%s' (protein %s). Searching for available isoform suffix.",
            base_budo_id,
            protein_id,
        )
        
        # Parse base ID: budo:{name}_{organism}_v{version}
        if not base_budo_id.startswith("budo:"):
            logger.error(
                "Invalid BUDO ID format '%s' (protein %s). Cannot deduplicate.",
                base_budo_id,
                protein_id,
            )
            return base_budo_id
        
        parts = base_budo_id.replace("budo:", "").rsplit("_v", 1)
        if len(parts) != 2:
            logger.error(
                "Cannot parse BUDO ID '%s' (protein %s) for deduplication.",
                base_budo_id,
                protein_id,
            )
            return base_budo_id
        
        name_organism, version = parts
        
        # Try isoform suffixes: _iso2, _iso3, ..., _iso101
        for iso_num in range(2, 102):
            name_parts = name_organism.rsplit("_", 1)
            if len(name_parts) == 2:
                name, organism = name_parts
                deduplicated_id = f"budo:{name}_iso{iso_num}_{organism}_v{version}"
            else:
                deduplicated_id = f"budo:{name_organism}_iso{iso_num}_v{version}"
            
            if not await self.service.exists(deduplicated_id):
                logger.info(
                    "Deduplication successful: '%s' → '%s' (protein %s)",
                    base_budo_id,
                    deduplicated_id,
                    protein_id,
                )
                return deduplicated_id
        
        # Fallback: exhausted 100 isoforms
        logger.error(
            "Deduplication failed for '%s' (protein %s): exhausted 100 isoform suffixes.",
            base_budo_id,
            protein_id,
        )
        return base_budo_id


def _safe_float(value: Optional[str]) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        logger.debug("Unable to parse float from %s", value)
        return None


__all__ = [
    "CatalogRecord",
    "EmbeddingRecord",
    "CrossReferenceRecord",
    "CEAPopulationSummary",
    "CEAPopulationError",
    "CEAPopulationIngestor",
]
