"""DLM-LMP Bridge for AgenticDriver MCP Integration.

This module bridges the gap between natural language queries and structured MCP tool calls
by leveraging:
- DLM (Document Literature Mapping) for entity extraction
- LMP (Linear Molecular Protocol) for biological schema validation
- EntityMapper for knowledge base linking

Architecture:
    User Query (text)
        ↓
    DLMEncoder → entities (proteins, domains, PTMs, organisms)
        ↓
    EntityMapper → KB IDs (UniProt, PDB, HGNC) + confidence
        ↓
    SlotFiller → MCP tool arguments
        ↓
    LMPSchemaValidator → pre-flight validation
        ↓
    MCP Tool Call (validated args)

Example:
    >>> bridge = DLMLMPBridge()
    >>> result = bridge.process_query(
    ...     "Fetch p53 DNA-binding domain structure",
    ...     tool_type="pdb"
    ... )
    >>> print(result.args)
    {'query': 'uniprot:P04637 domain:DNA-binding organism:human'}
    >>> print(result.confidence)
    0.95
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# Conditional imports
try:
    from mica.memory.dlm.encoder import DLMEncoder
    from mica.memory.dlm.config import DLMConfig
    DLM_AVAILABLE = True
except ImportError:
    logger.warning("DLM not available. Install with: pip install -e .[dlm]")
    DLM_AVAILABLE = False

try:
    from mica.memory.dlm.entity_mapper import EntityMapper, EntityMapping
    ENTITY_MAPPER_AVAILABLE = True
except ImportError:
    logger.warning("EntityMapper not available.")
    ENTITY_MAPPER_AVAILABLE = False
    # Create mock EntityMapping for tests
    from dataclasses import dataclass
    from typing import Optional, List
    
    @dataclass
    class EntityMapping:
        """Mock EntityMapping when entity_mapper unavailable."""
        text: str
        entity_type: str
        kb_id: Optional[str] = None
        kb_source: Optional[str] = None
        confidence: float = 0.0
        synonyms: List[str] = None
        
        def __post_init__(self):
            if self.synonyms is None:
                self.synonyms = []
        
        def is_mapped(self) -> bool:
            return self.kb_id is not None

try:
    from bsm.lmp.validator import LMPValidator
    from bsm.lmp.generator import LMPGenerator
    LMP_AVAILABLE = True
except ImportError:
    logger.warning("LMP not available.")
    LMP_AVAILABLE = False

# Context extraction from LMP v4 XML presets
try:
    from bsm.lmp.context_extractor import LMPv4ContextExtractor, BiologicalContext
    from bsm.lmp.preset_resolver import LMPPresetResolver, get_preset_resolver
    LMP_CONTEXT_AVAILABLE = True
except ImportError:
    logger.warning("LMP context extractor not available.")
    LMP_CONTEXT_AVAILABLE = False
    BiologicalContext = None  # type: ignore


@dataclass
class ExtractedEntities:
    """Entities extracted by DLM from query text."""
    
    protein_names: List[str] = field(default_factory=list)
    uniprot_ids: List[str] = field(default_factory=list)
    pdb_ids: List[str] = field(default_factory=list)
    gene_names: List[str] = field(default_factory=list)
    domains: List[str] = field(default_factory=list)
    ptms: List[str] = field(default_factory=list)  # {S-P:PKA}, {K-Ac:p300}
    organisms: List[str] = field(default_factory=list)
    ligands: List[str] = field(default_factory=list)  # (ATP), (ADP)
    interactions: List[str] = field(default_factory=list)  # <PPI>
    
    # NeSy markers for context
    nesy_markers: Dict[str, List[str]] = field(default_factory=dict)
    
    def has_explicit_ids(self) -> bool:
        """Check if explicit database IDs are present."""
        return bool(self.uniprot_ids or self.pdb_ids)
    
    def get_primary_protein(self) -> Optional[str]:
        """Get primary protein identifier (prefer UniProt > PDB > name)."""
        if self.uniprot_ids:
            return self.uniprot_ids[0]
        if self.pdb_ids:
            return self.pdb_ids[0]
        if self.protein_names:
            return self.protein_names[0]
        return None


@dataclass
class LinkedEntities:
    """Entities linked to knowledge bases by EntityMapper."""
    
    uniprot_mappings: List[EntityMapping] = field(default_factory=list)
    pdb_mappings: List[EntityMapping] = field(default_factory=list)
    gene_mappings: List[EntityMapping] = field(default_factory=list)
    
    # Confidence metrics
    avg_confidence: float = 0.0
    min_confidence: float = 1.0
    max_confidence: float = 0.0
    
    # Disambiguation state
    needs_clarification: bool = False
    ambiguous_entities: List[Tuple[str, List[EntityMapping]]] = field(default_factory=list)
    
    def is_high_confidence(self, threshold: float = 0.8) -> bool:
        """Check if all mappings exceed confidence threshold."""
        return self.min_confidence >= threshold
    
    def get_synonyms(self, entity_type: str = "protein") -> List[str]:
        """Get all synonyms for entity type."""
        synonyms = []
        if entity_type == "protein":
            for mapping in self.uniprot_mappings:
                synonyms.extend(mapping.synonyms)
        return list(set(synonyms))


# Task #5: PTM-Residue Compatibility Matrix
PTM_RESIDUE_COMPATIBILITY = {
    "phosphorylation": ["S", "T", "Y", "SER", "THR", "TYR"],
    "acetylation": ["K", "LYS"],
    "methylation": ["K", "R", "LYS", "ARG"],
    "ubiquitination": ["K", "LYS"],
    "sumoylation": ["K", "LYS"],
    "glycosylation": ["N", "S", "T", "ASN", "SER", "THR"],
    "palmitoylation": ["C", "CYS"],
    "myristoylation": ["G", "GLY"],
    "nitrosylation": ["C", "CYS"],
}


@dataclass
class BridgeResult:
    """Result from DLM-LMP bridge processing."""
    
    # Original query
    query: str
    tool_type: str
    
    # Extracted entities
    extracted: ExtractedEntities
    linked: LinkedEntities
    
    # Final tool arguments
    args: Dict[str, Any] = field(default_factory=dict)
    
    # Metadata
    confidence: float = 0.0
    needs_pre_search: bool = False
    search_query: Optional[str] = None
    validation_errors: List[str] = field(default_factory=list)
    
    # Clarification
    clarification_prompt: Optional[str] = None
    
    # Biological context from LMP v4 XML presets
    biological_context: Optional[Any] = None  # BiologicalContext when available
    context_system_prompt: Optional[str] = None  # Pre-built system prompt fragment
    suggested_tools: List[str] = field(default_factory=list)
    
    def is_ready_for_execution(self) -> bool:
        """Check if args are ready for MCP tool call."""
        return bool(self.args) and not self.validation_errors and not self.clarification_prompt
    
    def has_biological_context(self) -> bool:
        """Check if LMP biological context is available."""
        return self.biological_context is not None


class DLMLMPBridge:
    """Bridge between DLM entity extraction and MCP tool execution.
    
    Responsibilities:
    1. Extract entities from natural language using DLM
    2. Link entities to knowledge bases using EntityMapper
    3. Fill tool argument slots from linked entities
    4. Validate arguments against LMP schemas
    5. Generate search queries when IDs are missing
    6. Trigger clarification dialogs for ambiguous entities
    
    Usage:
        bridge = DLMLMPBridge()
        
        # Process query
        result = bridge.process_query(
            "Fetch p53 DNA-binding domain structure",
            tool_type="pdb"
        )
        
        if result.clarification_prompt:
            # Ask user for clarification
            user_choice = ask_user(result.clarification_prompt)
            result = bridge.resolve_clarification(result, user_choice)
        
        if result.needs_pre_search:
            # Execute search first
            search_results = execute_mcp_tool("pdb", "search", result.search_query)
            pdb_id = rank_and_select(search_results)
            result.args["pdb_id"] = pdb_id
        
        # Execute tool with validated args
        tool_result = execute_mcp_tool(result.tool_type, result.args)
    """
    
    def __init__(
        self,
        enable_dlm: bool = True,
        enable_entity_mapper: bool = True,
        enable_lmp_validation: bool = True,
        enable_lmp_context: bool = True,
        confidence_threshold: float = 0.8,
        disambiguation_delta: float = 0.2,
        preset_base: Optional[str] = None,
    ):
        """Initialize bridge.
        
        Args:
            enable_dlm: Use DLM for entity extraction (fallback to regex if False)
            enable_entity_mapper: Use EntityMapper for KB linking
            enable_lmp_validation: Validate args against LMP schemas
            enable_lmp_context: Load biological context from LMP v4 XML presets
            confidence_threshold: Minimum confidence for auto-linking (0-1)
            disambiguation_delta: Max confidence difference for disambiguation
            preset_base: Override path to output_all_presets/ directory
        """
        self.enable_dlm = enable_dlm and DLM_AVAILABLE
        self.enable_entity_mapper = enable_entity_mapper and ENTITY_MAPPER_AVAILABLE
        self.enable_lmp_validation = enable_lmp_validation and LMP_AVAILABLE
        self.enable_lmp_context = enable_lmp_context and LMP_CONTEXT_AVAILABLE
        
        self.confidence_threshold = confidence_threshold
        self.disambiguation_delta = disambiguation_delta
        
        # Initialize components
        if self.enable_dlm:
            try:
                self.dlm_encoder = DLMEncoder()
                logger.info("✅ DLMEncoder initialized")
            except Exception as e:
                logger.warning(f"Failed to initialize DLMEncoder: {e}")
                self.enable_dlm = False
        
        if self.enable_entity_mapper:
            try:
                self.entity_mapper = EntityMapper()
                logger.info("✅ EntityMapper initialized")
            except Exception as e:
                logger.warning(f"Failed to initialize EntityMapper: {e}")
                self.enable_entity_mapper = False
        
        if self.enable_lmp_validation:
            try:
                self.lmp_validator = LMPValidator()
                logger.info("✅ LMPValidator initialized")
            except Exception as e:
                logger.warning(f"Failed to initialize LMPValidator: {e}")
                self.enable_lmp_validation = False
        
        # LMP Context: preset resolver + extractor
        self.preset_resolver = None
        self.context_extractor = None
        self._context_cache: Dict[str, Any] = {}  # accession → BiologicalContext
        self._context_cache_max: int = 100  # LRU eviction cap — prevent unbounded memory in long processes
        self._context_cache_order: List[str] = []  # insertion-order tracker for LRU eviction
        
        if self.enable_lmp_context:
            try:
                self.preset_resolver = get_preset_resolver(preset_base)
                self.context_extractor = LMPv4ContextExtractor()
                logger.info("✅ LMP Context (PresetResolver + ContextExtractor) initialized")
            except Exception as e:
                logger.warning(f"Failed to initialize LMP context: {e}")
                self.enable_lmp_context = False
        
        # Tool-specific configurations
        self.tool_configs = self._load_tool_configs()
        
        logger.info(f"DLMLMPBridge initialized (DLM={self.enable_dlm}, "
                   f"EntityMapper={self.enable_entity_mapper}, "
                   f"LMP={self.enable_lmp_validation})")
    
    def process_query(
        self,
        query: str,
        tool_type: str,
        tool_schema: Optional[Dict[str, Any]] = None,
    ) -> BridgeResult:
        """Process natural language query into validated MCP tool arguments.
        
        Args:
            query: User query text
            tool_type: Target MCP server/tool type (pdb, uniprot, pubmed, etc.)
            tool_schema: Optional tool input schema for validation
        
        Returns:
            BridgeResult with extracted entities, linked IDs, and tool args
        """
        logger.info(f"Processing query for tool_type={tool_type}: {query[:100]}...")
        
        # Step 1: Extract entities using DLM
        extracted = self._extract_entities(query)
        logger.debug(f"Extracted entities: proteins={extracted.protein_names}, "
                    f"domains={extracted.domains}, uniprot={extracted.uniprot_ids}")
        
        # Step 1.5: Inject biological context from LMP v4 XML presets
        bio_context = None
        context_prompt = None
        context_tools: List[str] = []
        if self.enable_lmp_context:
            bio_context, context_prompt, context_tools = self._inject_biological_context(
                extracted, query
            )
        
        # Step 2: Link entities to knowledge bases
        linked = self._link_entities(extracted)
        logger.debug(f"Linked entities: confidence={linked.avg_confidence:.2f}, "
                    f"needs_clarification={linked.needs_clarification}")
        
        # Step 3: Check if clarification needed
        if linked.needs_clarification:
            clarification_prompt = self._generate_clarification_prompt(linked)
            return BridgeResult(
                query=query,
                tool_type=tool_type,
                extracted=extracted,
                linked=linked,
                clarification_prompt=clarification_prompt,
                biological_context=bio_context,
                context_system_prompt=context_prompt,
                suggested_tools=context_tools,
            )
        
        # Step 4: Fill tool argument slots
        args = self._fill_tool_args(extracted, linked, tool_type, tool_schema)
        logger.debug(f"Filled tool args: {args}")
        
        # Step 5: Check if pre-search needed
        needs_pre_search, search_query = self._check_needs_pre_search(
            extracted, linked, tool_type, args
        )
        
        # Step 6: Validate arguments (if LMP enabled)
        validation_errors = []
        if self.enable_lmp_validation and args:
            validation_errors = self._validate_args(tool_type, args)
        
        result = BridgeResult(
            query=query,
            tool_type=tool_type,
            extracted=extracted,
            linked=linked,
            args=args,
            confidence=linked.avg_confidence,
            needs_pre_search=needs_pre_search,
            search_query=search_query,
            validation_errors=validation_errors,
            biological_context=bio_context,
            context_system_prompt=context_prompt,
            suggested_tools=context_tools,
        )
        
        logger.info(f"Bridge result: ready={result.is_ready_for_execution()}, "
                   f"pre_search={needs_pre_search}, confidence={linked.avg_confidence:.2f}")
        
        return result
    
    def _inject_biological_context(
        self,
        extracted: ExtractedEntities,
        query: str,
    ) -> Tuple[Optional[Any], Optional[str], List[str]]:
        """Load biological context from LMP v4 XML presets for extracted entities.
        
        Resolves UniProt IDs (or gene names) to preset XML files, parses them,
        and returns structured biological context + system prompt fragment.
        
        Args:
            extracted: Entities extracted from query
            query: Original user query (for gene name fallback)
        
        Returns:
            Tuple of (BiologicalContext, system_prompt_fragment, suggested_tools)
        """
        if not self.enable_lmp_context or not self.preset_resolver:
            return None, None, []
        
        # Try UniProt IDs first
        for uid in extracted.uniprot_ids:
            ctx = self._load_context_for_accession(uid)
            if ctx:
                return ctx, ctx.to_system_prompt(), ctx.suggest_tools()
        
        # Try PDB IDs (resolves PDB-keyed presets like 2V3S_OSR1_*.xml)
        for pdb_id in extracted.pdb_ids:
            path = self.preset_resolver.resolve_by_pdb_id(pdb_id)
            if path:
                ctx = self._load_context_from_path(path)
                if ctx:
                    return ctx, ctx.to_system_prompt(), ctx.suggest_tools()
        
        # Try linked protein names via resolver gene name search
        for protein in extracted.protein_names:
            path = self.preset_resolver.resolve_by_gene_name(protein)
            if path:
                ctx = self._load_context_from_path(path)
                if ctx:
                    return ctx, ctx.to_system_prompt(), ctx.suggest_tools()
        
        # Try gene names
        for gene in extracted.gene_names:
            path = self.preset_resolver.resolve_by_gene_name(gene)
            if path:
                ctx = self._load_context_from_path(path)
                if ctx:
                    return ctx, ctx.to_system_prompt(), ctx.suggest_tools()
        
        logger.debug("No LMP preset matched for extracted entities")
        return None, None, []
    
    def _load_context_for_accession(self, accession: str) -> Optional[Any]:
        """Load and cache BiologicalContext for a UniProt accession.

        Uses a bounded LRU cache (max 100 entries) to prevent unbounded
        memory growth in long-running Railway processes.  Cache hits are
        logged for observability (``lmp_cache_hit``).
        """
        accession = accession.strip().upper()
        
        # Check cache
        if accession in self._context_cache:
            logger.info("lmp_cache_hit accession=%s", accession)
            # Move to end of LRU order
            try:
                self._context_cache_order.remove(accession)
            except ValueError:
                pass
            self._context_cache_order.append(accession)
            return self._context_cache[accession]
        
        # Resolve to file path
        path = self.preset_resolver.resolve(accession)
        if not path:
            return None
        
        ctx = self._load_context_from_path(path)
        if ctx:
            self._context_cache[accession] = ctx
            self._context_cache_order.append(accession)
            # LRU eviction when cache exceeds max size
            while len(self._context_cache_order) > self._context_cache_max:
                evicted = self._context_cache_order.pop(0)
                self._context_cache.pop(evicted, None)
        return ctx
    
    def _load_context_from_path(self, path: Path) -> Optional[Any]:
        """Load BiologicalContext from a preset XML file path."""
        try:
            ctx = self.context_extractor.extract_from_file(str(path))
            if ctx and ctx.uniprot_id:
                logger.info(
                    f"✅ Loaded LMP context for {ctx.uniprot_id} "
                    f"({ctx.protein_name}) from {path.name}"
                )
                # Also cache by accession
                self._context_cache[ctx.uniprot_id] = ctx
            return ctx
        except Exception as e:
            logger.warning(f"Failed to load LMP context from {path}: {e}")
            return None
    
    def get_biological_context(self, uniprot_id: str) -> Optional[Any]:
        """Public API: get BiologicalContext for a UniProt ID.
        
        Can be used by drivers to get context without going through
        the full process_query pipeline.
        """
        if not self.enable_lmp_context:
            return None
        return self._load_context_for_accession(uniprot_id)

    @staticmethod
    def _jsonable_context_value(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, dict):
            return {
                str(key): DLMLMPBridge._jsonable_context_value(inner)
                for key, inner in value.items()
            }
        if isinstance(value, (list, tuple, set)):
            return [DLMLMPBridge._jsonable_context_value(item) for item in value]
        if hasattr(value, "to_compact_dict") and callable(getattr(value, "to_compact_dict")):
            try:
                return DLMLMPBridge._jsonable_context_value(value.to_compact_dict())
            except Exception:
                return str(value)
        if hasattr(value, "to_dict") and callable(getattr(value, "to_dict")):
            try:
                return DLMLMPBridge._jsonable_context_value(value.to_dict())
            except Exception:
                return str(value)
        if hasattr(value, "__dict__"):
            try:
                return DLMLMPBridge._jsonable_context_value(vars(value))
            except Exception:
                return str(value)
        return str(value)

    @classmethod
    def _coerce_prior_context_payload(cls, context: Any) -> Dict[str, Any]:
        if context is None:
            return {}
        coerced = cls._jsonable_context_value(context)
        return coerced if isinstance(coerced, dict) else {}

    @classmethod
    def build_typed_prior_receipt(
        cls,
        context: Any,
        *,
        focused_entity: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build a compact, versioned BUDO/LMP prior receipt for DLM/ATOM.

        The receipt is advisory only: it can enrich an ambiguity route but it must
        never override deterministic accept/reject decisions.
        """
        payload = cls._coerce_prior_context_payload(context)
        focused = str(focused_entity or "").strip() or None

        keywords = [
            str(item).strip()
            for item in (payload.get("keywords") or [])
            if str(item).strip()
        ][:8]

        domains: List[Dict[str, Any]] = []
        for item in (payload.get("domains") or [])[:5]:
            if isinstance(item, dict):
                domain = {}
                for key in ("name", "type", "start", "end"):
                    if item.get(key) is not None:
                        domain[str(key)] = cls._jsonable_context_value(item.get(key))
                if domain:
                    domains.append(domain)
            elif str(item).strip():
                domains.append({"name": str(item).strip()})

        ptms: List[Dict[str, Any]] = []
        for item in (payload.get("ptms") or [])[:5]:
            if isinstance(item, dict):
                ptm = {}
                for key in ("type", "residue", "position"):
                    if item.get(key) is not None:
                        ptm[str(key)] = cls._jsonable_context_value(item.get(key))
                if ptm:
                    ptms.append(ptm)
            elif str(item).strip():
                ptms.append({"type": str(item).strip()})

        comments = payload.get("comments") or {}
        comment_sections = sorted(str(key) for key in comments.keys())[:8] if isinstance(comments, dict) else []
        function_excerpt: List[str] = []
        if isinstance(comments, dict):
            raw_function_entries = comments.get("FUNCTION") or comments.get("function") or []
            if isinstance(raw_function_entries, list):
                function_excerpt = [
                    str(item).strip()
                    for item in raw_function_entries
                    if str(item).strip()
                ][:3]

        suggested_tools: List[str] = []
        if hasattr(context, "suggest_tools") and callable(getattr(context, "suggest_tools")):
            try:
                suggested_tools = [
                    str(item).strip()
                    for item in (context.suggest_tools() or [])
                    if str(item).strip()
                ][:8]
            except Exception:
                suggested_tools = []

        receipt: Dict[str, Any] = {
            "contract_version": "dlm_lmp_prior_receipt_v1",
            "status": "absent",
            "source_plane": "budo_lmp",
            "source_kind": "lmp_biological_context",
            "owner_seam": "mica.drivers.dlm_lmp_bridge.DLMLMPBridge.build_typed_prior_receipt",
            "consumer_mode": "advisory_prior_only",
            "absence_behavior": "fail_closed_no_prior",
            "focused_entity": focused,
            "provenance": {
                "context_keys": sorted(payload.keys()),
                "source_uniprot_id": payload.get("uniprot_id"),
                "source_budo_id": payload.get("budo_id"),
            },
            "protein_summary": {},
            "feature_summary": {
                "keyword_count": len(payload.get("keywords") or []),
                "top_keywords": keywords,
                "domain_count": len(payload.get("domains") or []),
                "top_domains": domains,
                "ptm_count": len(payload.get("ptms") or []),
                "top_ptms": ptms,
                "comment_sections": comment_sections,
                "function_excerpt": function_excerpt,
            },
            "suggested_tools": suggested_tools,
        }

        has_identity = any(
            payload.get(key)
            for key in ("uniprot_id", "budo_id", "protein_name", "gene_names")
        )
        if not payload or not has_identity:
            return receipt

        gene_names = [
            str(item).strip()
            for item in (payload.get("gene_names") or [])
            if str(item).strip()
        ][:5]

        receipt["status"] = "present"
        receipt["protein_summary"] = {
            "uniprot_id": payload.get("uniprot_id"),
            "budo_id": payload.get("budo_id"),
            "protein_name": payload.get("protein_name"),
            "gene_names": gene_names,
            "organism": payload.get("organism"),
        }
        return receipt
    
    def get_available_preset_proteins(self) -> List[str]:
        """Return list of UniProt accessions with LMP presets on disk."""
        if not self.enable_lmp_context or not self.preset_resolver:
            return []
        return self.preset_resolver.get_available_proteins()

    def check_bucket_cache(
        self,
        user_id: str,
        uniprot_id: str,
        prefix: str = "lmp_v4",
    ) -> List[str]:
        """Check if LMP XMLs for *uniprot_id* already exist in the user bucket.

        Returns a list of matching object paths (empty if nothing cached).
        """
        try:
            from mica.storage.gcs_user_storage import get_storage_manager
            storage = get_storage_manager()
            objs = storage.list_objects(
                user_id=user_id,
                prefix=f"{prefix}/{uniprot_id}/",
                max_results=20,
            )
            paths = [o["name"] for o in objs if o["name"].endswith(".xml")]
            if paths:
                logger.info("Bucket cache hit for %s: %d XMLs", uniprot_id, len(paths))
            return paths
        except Exception as exc:
            logger.debug("Bucket cache check failed for %s: %s", uniprot_id, exc)
            return []
    
    def _extract_entities(self, query: str) -> ExtractedEntities:
        """Extract entities from query using DLM or fallback regex.
        
        Args:
            query: User query text
        
        Returns:
            ExtractedEntities with detected entities and NeSy markers
        """
        entities = ExtractedEntities()
        
        if self.enable_dlm:
            try:
                # Use DLM encoder
                encoded = self.dlm_encoder.encode(query)
                
                # Extract by entity type
                for entity in encoded.entities:
                    entity_type = entity.get("type", "").lower()
                    text = entity.get("text", "")
                    
                    if entity_type == "protein":
                        entities.protein_names.append(text)
                    elif entity_type == "gene":
                        entities.gene_names.append(text)
                    elif entity_type == "domain":
                        entities.domains.append(text)
                    # Add more entity types as needed
                
                # Extract NeSy markers from annotated text
                entities.nesy_markers = self._extract_nesy_markers(encoded.text)
                
            except Exception as e:
                logger.warning(f"DLM extraction failed: {e}, falling back to regex")
                self.enable_dlm = False
        
        # Fallback to regex if DLM disabled OR if DLM extracted nothing useful
        if not self.enable_dlm or not entities.has_explicit_ids():
            # Merge regex results with DLM results (in case DLM got some entities but missed IDs)
            regex_entities = self._extract_entities_regex(query)
            if not entities.has_explicit_ids():
                entities.uniprot_ids = regex_entities.uniprot_ids
                entities.pdb_ids = regex_entities.pdb_ids
            if not entities.protein_names:
                entities.protein_names = regex_entities.protein_names
            if not entities.gene_names:
                entities.gene_names = regex_entities.gene_names
            if not entities.domains:
                entities.domains = regex_entities.domains
            if not entities.nesy_markers:
                entities.nesy_markers = regex_entities.nesy_markers
        
        return entities
    
    def _extract_entities_regex(self, text: str) -> ExtractedEntities:
        """Fallback regex-based entity extraction (original implementation).
        
        Args:
            text: Query text
        
        Returns:
            ExtractedEntities with regex-detected IDs
        """
        entities = ExtractedEntities()
        
        # UniProt accessions
        uniprot_pattern = r"\b([OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9]{5})\b"
        entities.uniprot_ids = re.findall(uniprot_pattern, text)
        
        # PDB IDs — standard format: digit + letter + 2 alphanumeric (e.g. 2V3S, 6FBK)
        pdb_pattern = r"\b([0-9][A-Za-z][A-Za-z0-9]{2})\b"
        _PDB_FALSE_POSITIVES = {"1way", "2way", "3way", "4way", "1sec", "2min", "1mol", "2mol"}
        entities.pdb_ids = [
            m for m in re.findall(pdb_pattern, text)
            if m.lower() not in _PDB_FALSE_POSITIVES
        ]
        
        # Protein names (common patterns)
        protein_patterns = [
            r'\b([A-Z][A-Z0-9]{2,})\b',  # TP53, EGFR
            r'\bp\d+\b',                   # p53, p21
            r'\b([A-Z][a-z]+\d+)\b',       # Cdk2, Akt1
        ]
        for pattern in protein_patterns:
            matches = re.findall(pattern, text)
            entities.protein_names.extend(matches)
        
        # Normalize protein names (p53 → TP53)
        name_mapping = {
            "p53": "TP53",
            "p21": "CDKN1A",
            "p27": "CDKN1B",
        }
        entities.protein_names = [
            name_mapping.get(name.lower(), name)
            for name in entities.protein_names
        ]
        
        # Domain/region extraction
        domain_patterns = [
            r'(DNA-binding domain)',
            r'(kinase domain)',
            r'(SH[23] domain)',
            r'(tetramerization domain)',
        ]
        for pattern in domain_patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            entities.domains.extend(matches)
        
        # Deduplicate
        entities.protein_names = sorted(set(entities.protein_names))
        entities.uniprot_ids = sorted(set(entities.uniprot_ids))
        entities.pdb_ids = sorted(set(entities.pdb_ids))
        entities.domains = sorted(set(entities.domains))
        
        return entities
    
    def resolve_clarification(
        self,
        bridge_result: BridgeResult,
        user_choice: Dict[str, Any]
    ) -> BridgeResult:
        """Resolve ambiguity with user clarification.
        
        Task #6: Multi-turn clarification dialog
        
        Args:
            bridge_result: Original bridge result with ambiguity
            user_choice: User's selection (e.g., {"entity": "TP53", "mapping_index": 0})
        
        Returns:
            Updated BridgeResult with resolved entity
        """
        logger.info(f"Resolving clarification: {user_choice}")
        
        # Extract user selection
        entity_name = user_choice.get("entity")
        mapping_index = user_choice.get("mapping_index", 0)
        
        # Find ambiguous entity
        for ambig_entity, mappings in bridge_result.linked.ambiguous_entities:
            if ambig_entity == entity_name:
                # Select mapping
                if 0 <= mapping_index < len(mappings):
                    selected = mappings[mapping_index]
                    
                    # Update linked entities
                    if selected.entity_type == "protein":
                        bridge_result.linked.uniprot_mappings = [selected]
                    elif selected.entity_type == "gene":
                        bridge_result.linked.gene_mappings = [selected]
                    
                    # Clear ambiguity
                    bridge_result.linked.needs_clarification = False
                    bridge_result.linked.ambiguous_entities = []
                    
                    # Rebuild args with resolved entity
                    bridge_result.args = self._fill_tool_args(
                        bridge_result.extracted,
                        bridge_result.linked,
                        bridge_result.tool_type
                    )
                    
                    # Update confidence
                    bridge_result.confidence = selected.confidence
                    bridge_result.clarification_prompt = None
                    
                    logger.info(f"✅ Resolved to: {selected.kb_id} (confidence: {selected.confidence:.2f})")
                    break
        
        return bridge_result
    
    def _extract_nesy_markers(self, annotated_text: str) -> Dict[str, List[str]]:
        """Extract NeSy markers from DLM-annotated text.
        
        Task #7: Enhanced NeSy marker extraction for tool routing
        
        Args:
            annotated_text: Text with NeSy markers like [DOM:kinase], {S-P:PKA}
        
        Returns:
            Dictionary mapping marker type to values
        """
        markers = {
            "domains": [],
            "ptms": [],
            "ligands": [],
            "interactions": [],
            "evolutionary": [],
            "functional": [],
            "structural": [],
            "comparative": [],
            "dynamic": [],
        }
        
        # Domain markers: [DOM:kinase]
        dom_pattern = r'\[DOM:([^\]]+)\]'
        markers["domains"] = re.findall(dom_pattern, annotated_text)
        
        # PTM markers: {S-P:PKA}, {K-Ac:p300}
        ptm_pattern = r'\{([A-Z]-[A-Za-z]+:[^\}]+)\}'
        markers["ptms"] = re.findall(ptm_pattern, annotated_text)
        
        # Ligand markers: (ATP), (ADP)
        ligand_pattern = r'\(([A-Z]{3,})\)'
        markers["ligands"] = re.findall(ligand_pattern, annotated_text)
        
        # Interaction markers: <PPI>
        interaction_pattern = r'<([A-Z]+)>'
        markers["interactions"] = re.findall(interaction_pattern, annotated_text)
        
        # Task #7: Intent-based markers from plain text
        text_lower = annotated_text.lower()
        
        intent_patterns = {
            "evolutionary": [r"conserved", r"evolution", r"phylogen", r"homolog", r"ortholog"],
            "functional": [r"function", r"activity", r"catalys", r"binding", r"regulat"],
            "structural": [r"structure", r"fold", r"domain", r"motif", r"conformation"],
            "comparative": [r"compar", r"similar", r"differ", r"align", r"versus"],
            "dynamic": [r"dynamic", r"motion", r"flexibility", r"trajectory", r"simulation"],
        }
        
        for category, pattern_list in intent_patterns.items():
            for pattern in pattern_list:
                if re.search(rf"\b{pattern}\w*\b", text_lower):
                    matches = re.findall(rf"\b{pattern}\w*\b", text_lower)
                    markers[category].extend(matches)
        
        # Deduplicate
        for key in markers:
            markers[key] = sorted(set(markers[key]))
        
        return markers
    
    def suggest_tools_from_markers(self, nesy_markers: Dict[str, List[str]]) -> List[str]:
        """Suggest appropriate tools based on NeSy markers.
        
        Task #7: Tool routing based on query intent
        
        Args:
            nesy_markers: Detected NeSy markers by category
        
        Returns:
            List of suggested tool names
        """
        suggestions = []
        
        # Structural markers → PDB, AlphaFold
        if nesy_markers.get("structural") or nesy_markers.get("domains"):
            suggestions.extend(["pdb", "alphafold"])
        
        # Evolutionary markers → BLAST, phylogeny tools
        if nesy_markers.get("evolutionary"):
            suggestions.extend(["blast", "phylogeny"])
        
        # Functional markers → UniProt, GO annotations
        if nesy_markers.get("functional"):
            suggestions.extend(["uniprot", "gene_ontology"])
        
        # Dynamic markers → MD analysis
        if nesy_markers.get("dynamic"):
            suggestions.extend(["md_analysis", "trajectory"])
        
        # Comparative markers → alignment tools
        if nesy_markers.get("comparative"):
            suggestions.extend(["alignment", "rmsd"])
        
        # Interaction markers → PPI databases
        if nesy_markers.get("interactions"):
            suggestions.extend(["ppi", "complex"])
        
        # PTM markers → PTM databases
        if nesy_markers.get("ptms"):
            suggestions.extend(["ptm_db", "phosphosite"])
        
        return list(dict.fromkeys(suggestions))  # Deduplicate preserving order
    
    def _link_entities(self, extracted: ExtractedEntities) -> LinkedEntities:
        """Link extracted entities to knowledge bases using EntityMapper.
        
        Args:
            extracted: Entities extracted from query
        
        Returns:
            LinkedEntities with KB mappings and confidence scores
        """
        linked = LinkedEntities()
        
        try:
            # ALWAYS add explicit IDs first (confidence = 1.0)
            # These don't require EntityMapper
            for uniprot_id in extracted.uniprot_ids:
                linked.uniprot_mappings.append(EntityMapping(
                    text=uniprot_id,
                    entity_type="protein",
                    kb_id=uniprot_id,
                    kb_source="uniprot",
                    confidence=1.0
                ))
            
            for pdb_id in extracted.pdb_ids:
                linked.pdb_mappings.append(EntityMapping(
                    text=pdb_id,
                    entity_type="structure",
                    kb_id=pdb_id,
                    kb_source="pdb",
                    confidence=1.0
                ))
            
            # If EntityMapper disabled, skip name resolution
            if not self.enable_entity_mapper:
                # Calculate confidence from explicit IDs only
                all_mappings = linked.uniprot_mappings + linked.pdb_mappings
                if all_mappings:
                    confidences = [m.confidence for m in all_mappings if m.is_mapped()]
                    if confidences:
                        linked.avg_confidence = sum(confidences) / len(confidences)
                        linked.min_confidence = min(confidences)
                        linked.max_confidence = max(confidences)
                return linked
            
            # Prepare batch of entities to map (names without explicit IDs)
            entities_to_map = []
            
            # Map protein names to UniProt
            for protein in extracted.protein_names:
                if protein not in extracted.uniprot_ids:  # Don't re-map explicit IDs
                    entities_to_map.append((protein, "protein"))
            
            # Map gene names to HGNC
            for gene in extracted.gene_names:
                entities_to_map.append((gene, "gene"))
            
            # Batch map
            if entities_to_map:
                mappings = self.entity_mapper.map_batch(entities_to_map)
                
                for mapping in mappings:
                    if mapping.entity_type == "protein":
                        linked.uniprot_mappings.append(mapping)
                    elif mapping.entity_type == "gene":
                        linked.gene_mappings.append(mapping)
            
            # Calculate confidence metrics
            all_mappings = (linked.uniprot_mappings + linked.pdb_mappings + 
                           linked.gene_mappings)
            if all_mappings:
                confidences = [m.confidence for m in all_mappings if m.is_mapped()]
                if confidences:
                    linked.avg_confidence = sum(confidences) / len(confidences)
                    linked.min_confidence = min(confidences)
                    linked.max_confidence = max(confidences)
            
            # Check for disambiguation needs
            linked.needs_clarification, linked.ambiguous_entities = \
                self._check_disambiguation_needed(linked)
        
        except Exception as e:
            logger.error(f"Entity linking failed: {e}")
        
        return linked
    
    def _check_disambiguation_needed(
        self,
        linked: LinkedEntities
    ) -> Tuple[bool, List[Tuple[str, List[EntityMapping]]]]:
        """Check if entity disambiguation is needed.
        
        Args:
            linked: Linked entities with mappings
        
        Returns:
            (needs_clarification, ambiguous_entities)
        """
        # TODO: Implement disambiguation logic
        # For now, return False
        return False, []
    
    def _generate_clarification_prompt(self, linked: LinkedEntities) -> str:
        """Generate clarification prompt for ambiguous entities.
        
        Args:
            linked: Linked entities with ambiguous mappings
        
        Returns:
            Human-readable clarification prompt
        """
        # TODO: Implement clarification prompt generation
        return "Please clarify entity mapping."
    
    def _fill_tool_args(
        self,
        extracted: ExtractedEntities,
        linked: LinkedEntities,
        tool_type: str,
        tool_schema: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Fill tool argument slots from extracted and linked entities.
        
        Args:
            extracted: Extracted entities
            linked: Linked entities
            tool_type: Target tool type
            tool_schema: Tool input schema
        
        Returns:
            Dictionary of tool arguments
        """
        args = {}
        
        # Get tool config
        config = self.tool_configs.get(tool_type, {})
        schema = tool_schema or config.get("schema", {})
        
        # Get required and optional fields
        props = schema.get("properties", {})
        required = schema.get("required", [])
        
        # Fill arguments based on tool type
        if tool_type == "pdb":
            args = self._fill_pdb_args(extracted, linked, props, required)
        elif tool_type == "uniprot":
            args = self._fill_uniprot_args(extracted, linked, props, required)
        elif tool_type in ["pubmed", "semantic_scholar", "arxiv"]:
            args = self._fill_literature_args(extracted, linked, props, required)
        else:
            # Generic filling
            args = self._fill_generic_args(extracted, linked, props, required)
        
        return args
    
    def _fill_pdb_args(
        self,
        extracted: ExtractedEntities,
        linked: LinkedEntities,
        props: Dict[str, Any],
        required: List[str],
    ) -> Dict[str, Any]:
        """Fill PDB tool arguments.
        
        Args:
            extracted: Extracted entities
            linked: Linked entities
            props: Schema properties
            required: Required fields
        
        Returns:
            PDB tool arguments
        """
        args = {}
        
        # Try to fill pdb_id from explicit IDs
        if extracted.pdb_ids:
            args["pdb_id"] = extracted.pdb_ids[0]
            return args
        
        # If no PDB ID, we'll need pre-search
        # Leave args empty to trigger search logic
        
        return args
    
    def _fill_uniprot_args(
        self,
        extracted: ExtractedEntities,
        linked: LinkedEntities,
        props: Dict[str, Any],
        required: List[str],
    ) -> Dict[str, Any]:
        """Fill UniProt tool arguments."""
        args = {}
        
        # Try accession field
        if linked.uniprot_mappings:
            best_mapping = max(linked.uniprot_mappings, key=lambda m: m.confidence)
            if best_mapping.kb_id:
                args["accession"] = best_mapping.kb_id
        
        return args
    
    def _fill_literature_args(
        self,
        extracted: ExtractedEntities,
        linked: LinkedEntities,
        props: Dict[str, Any],
        required: List[str],
    ) -> Dict[str, Any]:
        """Fill literature search tool arguments.
        
        Enriched with LMP biological context when available.
        """
        args = {}
        
        # Build query string
        query_parts = []
        
        # Add protein names
        if extracted.protein_names:
            query_parts.append(extracted.protein_names[0])
        
        # Add domains
        if extracted.domains:
            query_parts.append(extracted.domains[0])
        
        # Add PTMs if present
        if extracted.ptms:
            query_parts.append("phosphorylation")  # Simplified
        
        # Enrich with biological context keywords from LMP preset
        primary_id = extracted.get_primary_protein()
        if primary_id and primary_id in self._context_cache:
            ctx = self._context_cache[primary_id]
            # Add gene name for better literature recall
            if hasattr(ctx, 'gene_names') and ctx.gene_names:
                for gn in ctx.gene_names[:2]:
                    if gn not in query_parts:
                        query_parts.append(gn)
            # Add organism for precision
            if hasattr(ctx, 'organism') and ctx.organism:
                query_parts.append(ctx.organism)
        
        if query_parts:
            args["query"] = " ".join(query_parts)
        
        return args
    
    def _fill_generic_args(
        self,
        extracted: ExtractedEntities,
        linked: LinkedEntities,
        props: Dict[str, Any],
        required: List[str],
    ) -> Dict[str, Any]:
        """Generic argument filling fallback."""
        args = {}
        
        # Try to fill query field
        if "query" in props:
            primary = extracted.get_primary_protein()
            if primary:
                args["query"] = primary
        
        return args
    
    def _check_needs_pre_search(
        self,
        extracted: ExtractedEntities,
        linked: LinkedEntities,
        tool_type: str,
        args: Dict[str, Any],
    ) -> Tuple[bool, Optional[str]]:
        """Check if pre-search is needed before tool execution.
        
        Args:
            extracted: Extracted entities
            linked: Linked entities
            tool_type: Tool type
            args: Current tool arguments
        
        Returns:
            (needs_pre_search, search_query)
        """
        # PDB fetch without explicit ID requires search
        if tool_type == "pdb" and not args.get("pdb_id"):
            search_query = self._build_structure_search_query(extracted, linked)
            return True, search_query
        
        return False, None
    
    def _build_structure_search_query(
        self,
        extracted: ExtractedEntities,
        linked: LinkedEntities,
    ) -> str:
        """Build structured PDB search query from entities.
        
        Args:
            extracted: Extracted entities
            linked: Linked entities
        
        Returns:
            Structured search query string
        """
        query_parts = []
        
        # Add UniProt ID if available
        if linked.uniprot_mappings:
            best_mapping = max(linked.uniprot_mappings, key=lambda m: m.confidence)
            if best_mapping.kb_id:
                query_parts.append(f"uniprot:{best_mapping.kb_id}")
        
        # Add domain constraint
        if extracted.domains:
            domain = extracted.domains[0]
            query_parts.append(f'domain:"{domain}"')
        
        # Add organism
        if extracted.organisms:
            organism = extracted.organisms[0]
            query_parts.append(f'organism:"{organism}"')
        elif linked.uniprot_mappings:
            # Default to human if UniProt mapping exists
            query_parts.append('organism:"Homo sapiens"')
        
        # Add method preference (X-ray > Cryo-EM > NMR)
        query_parts.append("method:X-ray")
        
        return " ".join(query_parts)
    
    def _validate_args(
        self,
        tool_type: str,
        args: Dict[str, Any]
    ) -> List[str]:
        """Validate tool arguments against LMP schemas.
        
        Task #5: LMP Schema Validator Implementation
        
        Args:
            tool_type: Tool type
            args: Tool arguments
        
        Returns:
            List of validation error messages
        """
        errors = []
        
        if not self.enable_lmp_validation:
            return errors
        
        # Task #5: PTM validation using compatibility matrix
        if tool_type == "ptm" and "operation" in args:
            ptm_errors = self._validate_ptm_operation(args)
            errors.extend(ptm_errors)
        
        # Validate required fields
        required_fields = self._get_required_fields(tool_type)
        for field in required_fields:
            if field not in args or not args[field]:
                errors.append(f"Missing required field: {field}")
        
        # Validate data types
        type_errors = self._validate_arg_types(tool_type, args)
        errors.extend(type_errors)
        
        return errors
    
    def _validate_ptm_operation(self, args: Dict[str, Any]) -> List[str]:
        """Validate PTM operation against residue compatibility.
        
        Task #5: PTM-specific validation
        
        Args:
            args: PTM tool arguments
        
        Returns:
            List of validation errors
        """
        errors = []
        
        ptm_type = args.get("ptm_type", "").lower()
        residue = args.get("residue", "").upper()
        
        if not ptm_type:
            errors.append("PTM type not specified")
            return errors
        
        if not residue:
            errors.append("Target residue not specified")
            return errors
        
        # Check compatibility
        compatible_residues = PTM_RESIDUE_COMPATIBILITY.get(ptm_type)
        if not compatible_residues:
            errors.append(f"Unknown PTM type: {ptm_type}")
            return errors
        
        # Normalize residue (handle both 1-letter and 3-letter codes)
        residue_normalized = residue[:3] if len(residue) > 1 else residue
        
        if residue_normalized not in compatible_residues:
            errors.append(
                f"PTM '{ptm_type}' incompatible with residue '{residue}'. "
                f"Compatible residues: {', '.join(compatible_residues)}"
            )
        
        return errors
    
    def _get_required_fields(self, tool_type: str) -> List[str]:
        """Get required fields for tool type.
        
        Task #5: Schema validation helper
        """
        required = {
            "pdb": ["pdb_id"],
            "uniprot": ["accession"],
            "alphafold": ["uniprot_id"],
            "ptm": ["ptm_type", "residue"],
        }
        return required.get(tool_type, [])
    
    def _validate_arg_types(self, tool_type: str, args: Dict[str, Any]) -> List[str]:
        """Validate argument data types.
        
        Task #5: Type validation
        """
        errors = []
        
        # Type specifications
        type_specs = {
            "pdb_id": str,
            "accession": str,
            "uniprot_id": str,
            "resolution_max": (int, float),
            "limit": int,
        }
        
        for key, expected_type in type_specs.items():
            if key in args:
                if not isinstance(args[key], expected_type):
                    errors.append(
                        f"Invalid type for '{key}': expected {expected_type.__name__}, "
                        f"got {type(args[key]).__name__}"
                    )
        
        return errors
    
    def _load_tool_configs(self) -> Dict[str, Dict[str, Any]]:
        """Load tool-specific configurations.
        
        Returns:
            Dictionary mapping tool type to config
        """
        # TODO: Load from config file
        # For now, return empty configs
        return {}


# Singleton instance
_bridge_instance: Optional[DLMLMPBridge] = None


def get_bridge() -> DLMLMPBridge:
    """Get singleton bridge instance."""
    global _bridge_instance
    if _bridge_instance is None:
        _bridge_instance = DLMLMPBridge()
    return _bridge_instance


# Exports for testing
__all__ = [
    "DLMLMPBridge",
    "ExtractedEntities",
    "LinkedEntities",
    "BridgeResult",
    "EntityMapping",
    "get_bridge",
]

# Re-export LMP context if available
if LMP_CONTEXT_AVAILABLE:
    __all__.extend(["BiologicalContext", "LMPPresetResolver"])

# Re-export EntityMapper if available for test mocking
if ENTITY_MAPPER_AVAILABLE:
    __all__.append("EntityMapper")
