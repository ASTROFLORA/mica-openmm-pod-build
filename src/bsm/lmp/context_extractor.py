"""LMP v4 Context Extractor — Parse v4 XML into structured biological context.

This module is the canonical v4 XML parser that extracts ALL biological context
from LMP XML presets for injection into conversations, driver prompts, and
specialist agent context.

Architecture:
    LMP XML → LMPv4ContextExtractor → BiologicalContext (dict-like)
                                    → Comments (FUNCTION, PTM, SUBUNIT, ...)
                                    → NeSyGrammar (annotated sequence)
                                    → KnowledgeGraph (edges, cross-refs)
                                    → Geometry (PDB features, domains, PTMs)

Usage:
    from bsm.lmp.context_extractor import LMPv4ContextExtractor

    extractor = LMPv4ContextExtractor()
    ctx = extractor.extract_from_file("path/to/preset.xml")
    
    # Inject into conversation
    system_prompt = ctx.to_system_prompt()
    
    # Get specific context
    function_desc = ctx.comments.get("FUNCTION", "")
    ptm_info = ctx.comments.get("PTM", "")
    
    # Get tool routing hints
    suggested_tools = ctx.suggest_tools()
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# LMP v4 namespace
LMP_NS = "http://ai-university.edu/lmp/v4.0"
NS_MAP = {"lmp": LMP_NS}


def _tag(local: str) -> str:
    """Build namespaced tag for ElementTree."""
    return f"{{{LMP_NS}}}{local}"


@dataclass
class ExtractedComment:
    """A single biological comment from LMP XML."""
    comment_type: str
    text: str
    pubmed_ids: List[str] = field(default_factory=list)

    def __str__(self) -> str:
        refs = f" [PMIDs: {', '.join(self.pubmed_ids)}]" if self.pubmed_ids else ""
        return f"[{self.comment_type}] {self.text}{refs}"


@dataclass
class KGEdge:
    """Knowledge Graph edge."""
    source: str
    target: str
    relation: str
    evidence: Optional[str] = None


@dataclass
class CrossReference:
    """Cross-reference to external database."""
    database: str
    identifier: str
    description: Optional[str] = None


@dataclass
class DomainInfo:
    """Protein domain annotation."""
    name: str
    domain_type: str
    start: int
    end: int


@dataclass
class PTMInfo:
    """Post-translational modification."""
    ptm_type: str
    residue: str
    position: int
    evidence: Optional[str] = None


@dataclass
class BindingSiteInfo:
    """Binding site annotation."""
    ligand: str
    residues: List[str] = field(default_factory=list)
    pdb_ids: List[str] = field(default_factory=list)
    pmids: List[str] = field(default_factory=list)


@dataclass
class GeometryInfo:
    """Structural geometry information."""
    pdb_id: Optional[str] = None
    resolution: Optional[float] = None
    chain_id: Optional[str] = None
    center_of_mass: Optional[Tuple[float, float, float]] = None
    radius_of_gyration: Optional[float] = None
    secondary_structure: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class VisualInfo:
    """A single visual resource (image/SVG/URL) declared in <Geometry>/<Visuals>.

    Contract consumed by the FE protein card StructureTab. Produced by the
    generator's ``_add_visuals_v4`` emitter. ``url`` is the heavy resource,
    ``preview_url`` is a cheap thumbnail for initial paint.
    """
    kind: str  # e.g. "af_cif", "pdb_assembly_jpeg", "flatprot_svg"
    source: str = ""  # alphafold | rcsb | pdbe | interpro | mica | flatprot
    url: str = ""
    preview_url: str = ""
    pdb_id: Optional[str] = None
    entry_id: Optional[str] = None
    avg_plddt: Optional[float] = None
    local_path: Optional[str] = None  # non-public; set by offline renderers


@dataclass
class BiologicalContext:
    """Full biological context extracted from an LMP v4 XML preset.
    
    This is the primary output of LMPv4ContextExtractor and contains ALL
    biological knowledge that should be injected into conversations and
    specialist agent prompts.
    """
    # Identity
    uniprot_id: str = ""
    protein_name: str = ""
    gene_names: List[str] = field(default_factory=list)
    organism: str = ""
    organism_id: str = ""
    entry_type: str = ""
    budo_id: str = ""
    secondary_accessions: List[str] = field(default_factory=list)

    # Semantics
    keywords: List[str] = field(default_factory=list)
    comments: Dict[str, List[str]] = field(default_factory=dict)
    nesy_grammar: str = ""

    # Structural
    sequence: str = ""
    sequence_length: int = 0
    domains: List[DomainInfo] = field(default_factory=list)
    ptms: List[PTMInfo] = field(default_factory=list)
    binding_sites: List[BindingSiteInfo] = field(default_factory=list)
    geometry: List[GeometryInfo] = field(default_factory=list)
    visuals: List[VisualInfo] = field(default_factory=list)
    chain_state: str = ""  # e.g. "Phosphorylated_Active"

    # Knowledge Graph
    kg_edges: List[KGEdge] = field(default_factory=list)
    cross_references: List[CrossReference] = field(default_factory=list)

    # Meta
    preset_type: str = ""  # "full", "semantic", "nesy-core", etc.
    version: str = ""
    source_file: str = ""

    def get_function_summary(self, max_chars: int = 2000) -> str:
        """Get the primary function description, truncated if needed."""
        functions = self.comments.get("FUNCTION", [])
        if not functions:
            return ""
        text = " ".join(functions)
        if len(text) > max_chars:
            text = text[:max_chars] + "..."
        return text

    def get_ptm_summary(self) -> str:
        """Get PTM information summary."""
        ptm_comments = self.comments.get("PTM", [])
        ptm_annotations = [
            f"{p.ptm_type} at {p.residue}{p.position}" for p in self.ptms
        ]
        parts = []
        if ptm_comments:
            parts.append("Literature: " + " ".join(ptm_comments))
        if ptm_annotations:
            parts.append("Annotations: " + ", ".join(ptm_annotations))
        return "\n".join(parts)

    def get_interaction_summary(self) -> str:
        """Get protein interaction information."""
        subunit_comments = self.comments.get("SUBUNIT", [])
        return " ".join(subunit_comments) if subunit_comments else ""

    def get_domain_summary(self) -> str:
        """Get domain architecture summary."""
        domain_comments = self.comments.get("DOMAIN", [])
        domain_annotations = [
            f"{d.name} ({d.domain_type}, {d.start}-{d.end})" for d in self.domains
        ]
        parts = []
        if domain_comments:
            parts.append("Literature: " + " ".join(domain_comments))
        if domain_annotations:
            parts.append("Domains: " + ", ".join(domain_annotations))
        return "\n".join(parts)

    def get_tissue_specificity(self) -> str:
        """Get tissue expression data."""
        tissues = self.comments.get("TISSUE SPECIFICITY", [])
        return " ".join(tissues) if tissues else ""

    def get_activity_regulation(self) -> str:
        """Get activity regulation info."""
        regs = self.comments.get("ACTIVITY REGULATION", [])
        return " ".join(regs) if regs else ""

    def suggest_tools(self) -> List[str]:
        """Suggest appropriate MCP tools based on extracted context."""
        suggestions = []

        # Structure available → PDB tools
        if self.geometry:
            suggestions.append("pdb")
            suggestions.append("alphafold")

        # PTM annotations → phosphosite tools
        if self.ptms or self.comments.get("PTM"):
            suggestions.append("phosphosite")
            suggestions.append("ptm_db")

        # Interactions → PPI tools
        if self.comments.get("SUBUNIT") or self.kg_edges:
            suggestions.append("string_db")
            suggestions.append("intact")

        # Kinase keywords → kinase-specific tools
        kinase_keywords = {"kinase", "phosphorylation", "transferase"}
        if kinase_keywords & {kw.lower() for kw in self.keywords}:
            suggestions.append("kinase_db")

        # Drug-related cross-references
        drug_dbs = {"DrugBank", "ChEMBL", "PubChem"}
        if any(xr.database in drug_dbs for xr in self.cross_references):
            suggestions.append("drugbank")
            suggestions.append("chembl")

        # Binding sites → docking tools
        if self.binding_sites:
            suggestions.append("molecular_docking")

        return list(dict.fromkeys(suggestions))

    def to_system_prompt(self, max_tokens: int = 4000) -> str:
        """Generate a system prompt injection with biological context.
        
        Args:
            max_tokens: Approximate max character budget (not actual tokens)
            
        Returns:
            Formatted string for system prompt injection
        """
        parts = []
        budget = max_tokens

        # Header
        header = (
            f"=== Biological Context: {self.protein_name} ({self.uniprot_id}) ===\n"
            f"Organism: {self.organism} | Genes: {', '.join(self.gene_names)}\n"
            f"Keywords: {', '.join(self.keywords[:15])}\n"
        )
        parts.append(header)
        budget -= len(header)

        # Function (highest priority)
        func = self.get_function_summary(max_chars=min(budget // 2, 2000))
        if func:
            block = f"\n[FUNCTION]\n{func}\n"
            parts.append(block)
            budget -= len(block)

        # Activity regulation
        if budget > 500:
            reg = self.get_activity_regulation()
            if reg:
                block = f"\n[ACTIVITY REGULATION]\n{reg[:min(budget // 4, 800)]}\n"
                parts.append(block)
                budget -= len(block)

        # PTM
        if budget > 400:
            ptm = self.get_ptm_summary()
            if ptm:
                block = f"\n[PTM]\n{ptm[:min(budget // 4, 600)]}\n"
                parts.append(block)
                budget -= len(block)

        # Interactions
        if budget > 400:
            interact = self.get_interaction_summary()
            if interact:
                block = f"\n[INTERACTIONS]\n{interact[:min(budget // 4, 600)]}\n"
                parts.append(block)
                budget -= len(block)

        # Domain architecture
        if budget > 300:
            dom = self.get_domain_summary()
            if dom:
                block = f"\n[DOMAINS]\n{dom[:min(budget // 4, 500)]}\n"
                parts.append(block)
                budget -= len(block)

        # Tissue specificity
        if budget > 200:
            tissue = self.get_tissue_specificity()
            if tissue:
                block = f"\n[TISSUE SPECIFICITY]\n{tissue[:300]}\n"
                parts.append(block)
                budget -= len(block)

        # Structural summary
        if budget > 200 and self.geometry:
            geo = self.geometry[0]
            block = (
                f"\n[STRUCTURE]\n"
                f"PDB: {geo.pdb_id or 'N/A'} | "
                f"Resolution: {geo.resolution or 'N/A'}Å | "
                f"Chain: {geo.chain_id or 'N/A'}\n"
            )
            parts.append(block)

        return "".join(parts)

    def to_compact_dict(self) -> Dict[str, Any]:
        """Return a compact dictionary suitable for JSON serialization."""
        return {
            "uniprot_id": self.uniprot_id,
            "protein_name": self.protein_name,
            "gene_names": self.gene_names,
            "organism": self.organism,
            "keywords": self.keywords,
            "comments": self.comments,
            "domains": [
                {"name": d.name, "type": d.domain_type, "start": d.start, "end": d.end}
                for d in self.domains
            ],
            "ptms": [
                {"type": p.ptm_type, "residue": p.residue, "position": p.position}
                for p in self.ptms
            ],
            "binding_sites": [
                {"ligand": b.ligand, "residues": b.residues}
                for b in self.binding_sites
            ],
            "cross_references": [
                {"db": xr.database, "id": xr.identifier}
                for xr in self.cross_references
            ],
            "sequence_length": self.sequence_length,
            "chain_state": self.chain_state,
            "visuals": [
                {
                    "kind": v.kind,
                    "source": v.source,
                    "url": v.url,
                    "preview_url": v.preview_url,
                    "pdb_id": v.pdb_id,
                    "entry_id": v.entry_id,
                    "avg_plddt": v.avg_plddt,
                }
                for v in self.visuals
            ],
            "suggested_tools": self.suggest_tools(),
        }


class LMPv4ContextExtractor:
    """Parse LMP v4 XML and extract structured biological context.
    
    This is the canonical parser for v4 `<lmp:LMP>` XML files. It replaces
    the legacy v2-only parser.py and budo_parser.py which cannot handle v4.
    
    Usage:
        extractor = LMPv4ContextExtractor()
        ctx = extractor.extract_from_file("path/to/Q9H4A3_WNK1_full.xml")
        prompt = ctx.to_system_prompt()
    """

    def extract_from_file(self, xml_path: str | Path) -> BiologicalContext:
        """Extract biological context from an LMP v4 XML file.
        
        Args:
            xml_path: Path to the XML file
            
        Returns:
            BiologicalContext with all extracted information
        """
        xml_path = Path(xml_path)
        if not xml_path.exists():
            logger.warning(f"LMP XML not found: {xml_path}")
            return BiologicalContext(source_file=str(xml_path))

        try:
            tree = ET.parse(str(xml_path))
            root = tree.getroot()
            ctx = self._extract_from_element(root)
            ctx.source_file = str(xml_path)
            return ctx
        except ET.ParseError as e:
            logger.error(f"Failed to parse LMP XML {xml_path}: {e}")
            return BiologicalContext(source_file=str(xml_path))

    def extract_from_string(self, xml_string: str) -> BiologicalContext:
        """Extract from an XML string."""
        try:
            root = ET.fromstring(xml_string)
            return self._extract_from_element(root)
        except ET.ParseError as e:
            logger.error(f"Failed to parse LMP XML string: {e}")
            return BiologicalContext()

    def _extract_from_element(self, root: ET.Element) -> BiologicalContext:
        """Extract all context from parsed XML root."""
        ctx = BiologicalContext()

        # Extract version and preset type from root attributes
        ctx.version = root.get("version", "")
        ctx.preset_type = root.get("preset", "")

        # Identity
        self._extract_identity(root, ctx)

        # Semantics
        self._extract_semantics(root, ctx)

        # Geometry
        self._extract_geometry(root, ctx)

        # KnowledgeGraph
        self._extract_knowledge_graph(root, ctx)

        return ctx

    def _extract_identity(self, root: ET.Element, ctx: BiologicalContext) -> None:
        """Extract Identity block."""
        identity = root.find(_tag("Identity"))
        if identity is None:
            return

        # Primary Accession
        acc = identity.find(_tag("PrimaryAccession"))
        if acc is not None and acc.text:
            ctx.uniprot_id = acc.text.strip()

        # BudoID
        budo = identity.find(_tag("BudoID"))
        if budo is not None and budo.text:
            ctx.budo_id = budo.text.strip()

        # UniProtKBId
        kb_id = identity.find(_tag("UniProtKBId"))
        if kb_id is not None and kb_id.text:
            pass  # Stored in budo_id mostly

        # Entry Type
        entry = identity.find(_tag("EntryType"))
        if entry is not None and entry.text:
            ctx.entry_type = entry.text.strip()

        # Organism
        org = identity.find(_tag("Organism"))
        if org is not None:
            ctx.organism = org.text.strip() if org.text else ""
            ctx.organism_id = org.get("id", "")

        # Lineages (skip for now, low priority)

        # Secondary Accessions
        sec_acc = identity.find(_tag("SecondaryAccessions"))
        if sec_acc is not None:
            for val in sec_acc.findall(_tag("Value")):
                if val.text:
                    ctx.secondary_accessions.append(val.text.strip())

    def _extract_semantics(self, root: ET.Element, ctx: BiologicalContext) -> None:
        """Extract Semantics block — the richest biological context source."""
        semantics = root.find(_tag("Semantics"))
        if semantics is None:
            return

        # Protein Name
        pn = semantics.find(_tag("ProteinName"))
        if pn is not None and pn.text:
            ctx.protein_name = pn.text.strip()

        # Genes
        genes = semantics.find(_tag("Genes"))
        if genes is not None:
            for val in genes.findall(_tag("Value")):
                if val.text:
                    ctx.gene_names.append(val.text.strip())

        # Keywords
        keywords = semantics.find(_tag("Keywords"))
        if keywords is not None:
            for val in keywords.findall(_tag("Value")):
                if val.text:
                    ctx.keywords.append(val.text.strip())

        # NeSyGrammar
        nesy = semantics.find(_tag("NeSyGrammar"))
        if nesy is not None and nesy.text:
            ctx.nesy_grammar = nesy.text.strip()

        # Comments (FUNCTION, PTM, SUBUNIT, TISSUE SPECIFICITY, etc.)
        for comment in semantics.findall(_tag("Comment")):
            comment_type = comment.get("type", "UNKNOWN")
            text = comment.text.strip() if comment.text else ""
            if text:
                if comment_type not in ctx.comments:
                    ctx.comments[comment_type] = []
                ctx.comments[comment_type].append(text)

    def _extract_geometry(self, root: ET.Element, ctx: BiologicalContext) -> None:
        """Extract Geometry block."""
        geometry = root.find(_tag("Geometry"))
        if geometry is None:
            return

        # Sequence
        seq = geometry.find(_tag("Sequence"))
        if seq is not None and seq.text:
            ctx.sequence = seq.text.strip()
            length_attr = seq.get("length")
            if length_attr:
                try:
                    ctx.sequence_length = int(length_attr)
                except ValueError:
                    ctx.sequence_length = len(ctx.sequence)

        # Features (secondary structure, PDB data, geometry)
        geo_info = GeometryInfo()
        for feature in geometry.findall(_tag("Feature")):
            feat_type = feature.get("type", "")
            desc = feature.get("description", "")

            if feat_type.startswith("pdb:resolution"):
                match = re.search(r"resolution=([\d.]+)", desc)
                if match:
                    geo_info.resolution = float(match.group(1))
                pdb_match = re.search(r"pdb=(\w+)", desc)
                if pdb_match:
                    geo_info.pdb_id = pdb_match.group(1)

            elif feat_type.startswith("pdb:cell"):
                pdb_match = re.search(r"pdb=(\w+)", desc)
                if pdb_match and not geo_info.pdb_id:
                    geo_info.pdb_id = pdb_match.group(1)

            elif feat_type.startswith("geometry:com"):
                match = re.search(r"x=([\d.-]+)\s+y=([\d.-]+)\s+z=([\d.-]+)", desc)
                if match:
                    geo_info.center_of_mass = (
                        float(match.group(1)),
                        float(match.group(2)),
                        float(match.group(3)),
                    )

            elif feat_type.startswith("geometry:rg"):
                match = re.search(r"rg=([\d.]+)", desc)
                if match:
                    geo_info.radius_of_gyration = float(match.group(1))

            elif feat_type.startswith("secondary_structure"):
                geo_info.secondary_structure.append({
                    "type": feat_type,
                    "description": desc,
                })

        if geo_info.pdb_id or geo_info.resolution:
            ctx.geometry.append(geo_info)

        # Visuals (v4.3) — AF2/PDB/InterPro/FlatProt image URLs
        visuals = geometry.find(_tag("Visuals"))
        if visuals is not None:
            for v in visuals.findall(_tag("Visual")):
                kind = v.get("kind", "").strip()
                if not kind:
                    continue
                plddt_raw = v.get("avg_plddt")
                try:
                    plddt_val = float(plddt_raw) if plddt_raw else None
                except ValueError:
                    plddt_val = None
                ctx.visuals.append(VisualInfo(
                    kind=kind,
                    source=v.get("source", ""),
                    url=v.get("url", ""),
                    preview_url=v.get("preview_url", ""),
                    pdb_id=v.get("pdb_id") or None,
                    entry_id=v.get("entry_id") or None,
                    avg_plddt=plddt_val,
                    local_path=v.get("local_path") or None,
                ))

        # Chains
        for chain in geometry.findall(_tag("Chain")):
            chain_id = chain.get("id", "")
            state = chain.get("state", "")
            if state:
                ctx.chain_state = state
            if chain_id and ctx.geometry:
                ctx.geometry[0].chain_id = chain_id

            # Domains within chain
            for dom in chain.findall(_tag("Domain")):
                name = dom.get("name", "")
                dtype = dom.get("type", "")
                start = int(dom.get("start", "0"))
                end = int(dom.get("end", "0"))
                if name:
                    ctx.domains.append(DomainInfo(
                        name=name, domain_type=dtype, start=start, end=end
                    ))

            # PTMs within chain
            for ptm in chain.findall(_tag("PTM")):
                ptype = ptm.get("type", "")
                residue = ptm.get("residue", "")
                position = int(ptm.get("position", "0"))
                evidence = ptm.get("evidence", "")
                if ptype:
                    ctx.ptms.append(PTMInfo(
                        ptm_type=ptype, residue=residue,
                        position=position, evidence=evidence
                    ))

            # BindingSites within chain
            for bs in chain.findall(_tag("BindingSite")):
                ligand = bs.get("ligand", "")
                residues = []
                for res in bs.findall(_tag("Residue")):
                    if res.text:
                        residues.append(res.text.strip())
                ctx.binding_sites.append(BindingSiteInfo(
                    ligand=ligand, residues=residues
                ))

    def _extract_knowledge_graph(self, root: ET.Element, ctx: BiologicalContext) -> None:
        """Extract KnowledgeGraph block."""
        kg = root.find(_tag("KnowledgeGraph"))
        if kg is None:
            return

        # Edges
        for edge in kg.findall(_tag("Edge")):
            source = edge.get("source", "")
            target = edge.get("target", "")
            relation = edge.get("relation", "")
            evidence = edge.get("evidence", "")
            if source and target:
                ctx.kg_edges.append(KGEdge(
                    source=source, target=target,
                    relation=relation, evidence=evidence
                ))

        # CrossReferences
        for xref in kg.findall(_tag("CrossReference")):
            db = xref.get("database", "") or xref.get("db", "")
            identifier = xref.get("identifier", "") or xref.get("id", "")
            desc = xref.get("description", "")
            if db and identifier:
                ctx.cross_references.append(CrossReference(
                    database=db, identifier=identifier, description=desc
                ))

    def extract_nesy_markers(self, nesy_grammar: str) -> Dict[str, List[str]]:
        """Parse NeSyGrammar string and extract structured markers.
        
        This provides the reverse-parsing that nesy_encoder.parse_nesy_sequence()
        was supposed to implement (currently NotImplementedError).
        
        Args:
            nesy_grammar: NeSyGrammar annotated sequence string
            
        Returns:
            Dictionary mapping marker type → list of values
        """
        markers: Dict[str, List[str]] = {
            "domains": [],
            "ptms": [],
            "binding_sites": [],
            "motifs": [],
            "ppi_interfaces": [],
        }

        if not nesy_grammar:
            return markers

        # [DOM:name] ... [/DOM]
        dom_pattern = r'\[DOM:([^\]]+)\]'
        markers["domains"] = sorted(set(re.findall(dom_pattern, nesy_grammar)))

        # {P} phosphorylation sites, {S-P:kinase}
        ptm_pattern = r'\{([^}]+)\}'
        raw_ptms = re.findall(ptm_pattern, nesy_grammar)
        markers["ptms"] = sorted(set(raw_ptms))

        # [BIND:ligand|PMID:...|PDB:...|ChEBI:...]
        bind_pattern = r'\[BIND:([^\]]+)\]'
        raw_binds = re.findall(bind_pattern, nesy_grammar)
        for bind in raw_binds:
            parts = bind.split("|")
            ligand = parts[0] if parts else bind
            markers["binding_sites"].append(ligand)
        markers["binding_sites"] = sorted(set(markers["binding_sites"]))

        # [MOT:name]
        mot_pattern = r'\[MOT:([^\]]+)\]'
        markers["motifs"] = sorted(set(re.findall(mot_pattern, nesy_grammar)))

        # <PPI:...> or *ACTIVE* etc.
        ppi_pattern = r'<([A-Z]+)>'
        markers["ppi_interfaces"] = sorted(set(re.findall(ppi_pattern, nesy_grammar)))

        return markers


# Convenience singleton
_extractor: Optional[LMPv4ContextExtractor] = None


def get_context_extractor() -> LMPv4ContextExtractor:
    """Get singleton context extractor."""
    global _extractor
    if _extractor is None:
        _extractor = LMPv4ContextExtractor()
    return _extractor


__all__ = [
    "LMPv4ContextExtractor",
    "BiologicalContext",
    "ExtractedComment",
    "KGEdge",
    "CrossReference",
    "DomainInfo",
    "PTMInfo",
    "BindingSiteInfo",
    "GeometryInfo",
    "get_context_extractor",
]
