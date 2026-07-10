"""
BudoGraphWriter - Timescale GraphRAG Persistence Layer
========================================================

Persists BUDO V3 objects to TimescaleDB GraphRAG tables:
- atom_graph_nodes: Proteins, domains
- atom_graph_edges: PTMs, ligands, interfaces (hypertable)
- atom_facts: Motifs, catalytic residues

Author: Alex Rodriguez AI Lab
Phase: 0.002 - BUDO GraphRAG Integration
Date: January 21, 2026
Version: 2.0.0
"""

import asyncio
import logging
import uuid
from typing import Any, Dict, List, Optional, TYPE_CHECKING

import asyncpg
from asyncpg import Connection, Pool

from mica.infrastructure.persistence.pg_async import create_asyncpg_pool_for_database_url
from mica.infrastructure.persistence.timescale_graphrag_store import TimescaleGraphRAGStore

if TYPE_CHECKING:
    from bsm.schemas.budo_v3 import BudoV3

logger = logging.getLogger(__name__)


# R23.5 (CCC MF-3 closure, write-side): derive edge confidence from upstream
# signal richness rather than hardcoding 1.0 (F-037 / GAP-MEM-06).
# Empty-metadata fallback is intentionally NOT 1.0.
_BUDO_EDGE_DEFAULT_CONFIDENCE: float = 0.7


def _resolve_edge_confidence(
    metadata: Optional[Dict[str, Any]],
    base: float = _BUDO_EDGE_DEFAULT_CONFIDENCE,
    *,
    enzyme_bonus: float = 0.15,
    binding_bonus: float = 0.10,
    haddock_threshold: float = -100.0,
    haddock_bonus: float = 0.10,
) -> float:
    """Derive a non-degenerate confidence score from BUDO edge metadata.

    The function returns ``base`` when metadata is empty (deliberately != 1.0)
    and adds bounded bonuses for the presence of authoritative annotations
    (named enzyme, structurally-resolved binding residues, strong HADDOCK
    score). Capped at 1.0. Pure function, no I/O, no new imports beyond
    ``Dict``/``Optional`` already imported above.
    """
    score = float(base)
    if not metadata:
        return max(0.0, min(1.0, score))
    if metadata.get("enzyme"):
        score += enzyme_bonus
    binding_residues = metadata.get("binding_residues")
    try:
        if binding_residues and len(binding_residues) >= 3:
            score += binding_bonus
    except TypeError:
        pass
    haddock = metadata.get("haddock_score")
    try:
        if haddock is not None and float(haddock) <= haddock_threshold:
            score += haddock_bonus
    except (TypeError, ValueError):
        pass
    return max(0.0, min(1.0, score))


class BudoGraphWriter:
    """
    Writes BUDO V3 objects to TimescaleDB GraphRAG.
    
    Architecture:
    - Proteins → atom_graph_nodes (node_type='protein')
    - Domains → atom_graph_nodes (node_type='domain')
    - PTMs → atom_graph_edges (relationship='phosphorylates', 'acetylates', etc.)
    - Ligands → atom_graph_edges (relationship='binds_ligand')
    - Interfaces → atom_graph_edges (relationship='interacts_with')
    - Motifs/Catalytic → atom_facts (fact_type='structural_feature')
    """

    def __init__(self, pool: Pool, graph_store: Optional[TimescaleGraphRAGStore] = None):
        """
        Initialize writer with asyncpg connection pool.
        
        Args:
            pool: asyncpg.Pool instance connected to Timescale
        """
        self.pool = pool
        self.graph_store = graph_store or TimescaleGraphRAGStore(database_url="postgresql://pooled", pool=pool)

    async def upsert_budo_as_graph(
        self, 
        budo: "BudoV3",
        user_id: Optional[str] = None,
        session_id: Optional[str] = None
    ) -> str:
        """
        Persist complete BUDO V3 object as graph structure.
        
        Pipeline:
        1. Upsert protein node (atom_graph_nodes)
        2. Upsert domain nodes (atom_graph_nodes)
        3. Insert PTM edges (atom_graph_edges)
        4. Insert ligand edges (atom_graph_edges)
        5. Insert interface edges (atom_graph_edges)
        6. Insert motif/catalytic facts (atom_facts)
        
        Args:
            budo: BudoV3 object from parser
            user_id: Optional user for multi-tenancy
            session_id: Optional session for MUDO tracking
            
        Returns:
            node_id: UUID of protein node
            
        Raises:
            asyncpg.PostgresError: On database errors
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # 1. Upsert protein node
                protein_node_id = await self._upsert_protein_node(
                    conn, budo, user_id, session_id
                )
                
                # 2. Upsert domain nodes + create protein-domain edges
                domain_node_ids = await self._upsert_domain_nodes(
                    conn, budo, protein_node_id, user_id, session_id
                )
                
                # 3. Insert PTM edges (domain → PTM)
                await self._insert_ptm_edges(
                    conn, budo, domain_node_ids, user_id, session_id
                )
                
                # 4. Insert ligand edges (domain → ligand)
                await self._insert_ligand_edges(
                    conn, budo, domain_node_ids, user_id, session_id
                )
                
                # 5. Insert interface edges (protein → protein)
                await self._insert_interface_edges(
                    conn, budo, protein_node_id, user_id, session_id
                )
                
                # 6. Insert motifs and catalytic residues as facts
                await self._insert_structural_facts(
                    conn, budo, protein_node_id, user_id, session_id
                )

                # 7. Generate LMP XML artifact from BUDO state (BSM-001)
                try:
                    lmp_xml = budo.to_lmp_xml()
                    await self._persist_lmp_xml_fact(
                        conn, budo, lmp_xml, user_id, session_id
                    )
                except Exception as exc:
                    logger.warning("LMP XML generation skipped: %s", exc)
                
                return str(protein_node_id)

    async def _persist_lmp_xml_fact(
        self,
        conn: Connection,
        budo: "BudoV3",
        lmp_xml: str,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> None:
        """Persist the generated LMP XML as an atom_fact for provenance."""
        await self.graph_store.upsert_fact(
            content=lmp_xml,
            fact_type="lmp_xml_artifact",
            topic=budo.canonical_name,
            entities=[budo.budoId, budo.canonical_name],
            confidence=1.0,
            source_doi=None,
            user_id=user_id,
            session_id=session_id,
            metadata={
                "source": "budo_to_lmp_xml",
                "budo_id": budo.budoId,
                "domains": len(budo.domains),
                "interfaces": len(budo.interfaces),
            },
            conn=conn,
        )
        logger.info("LMP XML artifact persisted for %s (%d bytes)", budo.budoId, len(lmp_xml))

    async def _upsert_protein_node(
        self, 
        conn: Connection, 
        budo: "BudoV3",
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> uuid.UUID:
        """
        Upsert protein as node in atom_graph_nodes.
        
        Fields:
        - canonical_name: BUDO canonical name
        - node_type: 'protein'
        - aliases: []
        - description: Sequence summary
        - embedding: NULL (will be populated by embedding pipeline)
        - external_ids: {uniprot, pdb, chembl}
        - properties: {organism, taxonomy_id, sequence_length, functional_state}
        """
        # Extract external IDs from cross_references
        external_ids = {}
        for ref in budo.cross_references:
            if ref.database.lower() == 'uniprot':
                external_ids['uniprot'] = ref.identifier
            elif ref.database.lower() == 'pdb':
                if 'pdb' not in external_ids:
                    external_ids['pdb'] = []
                external_ids['pdb'].append(ref.identifier)
            elif ref.database.lower() == 'chembl':
                external_ids['chembl'] = ref.identifier
        
        # Properties dict
        properties = {
            'budo_id': budo.budoId,
            'recommended_name': budo.recommended_name,
            'organism': budo.organism,
            'taxonomy_id': budo.taxonomy_id,
            'sequence_length': budo.sequence_length,
            'molecular_weight': budo.molecular_weight,
            'isoelectric_point': budo.isoelectric_point,
            'functional_state': budo.functionalState.current.value,
            'go_terms': budo.go_terms,
            'kegg_pathways': budo.kegg_pathways,
            'reactome_pathways': budo.reactome_pathways,
            'ec_numbers': budo.ec_numbers,
        }
        if user_id:
            properties['user_id'] = user_id
        if session_id:
            properties['session_id'] = session_id
        
        # Description for embedding (sequence summary)
        description = (
            f"{budo.recommended_name} ({budo.canonical_name}) from {budo.organism}. "
            f"Sequence length: {budo.sequence_length} aa. "
            f"Functional state: {budo.functionalState.current.value}."
        )
        
        await self.graph_store.upsert_node(
            canonical_name=budo.canonical_name,
            node_type='protein',
            aliases=[],
            description=description,
            embedding=None,
            external_ids=external_ids,
            properties=properties,
            source_doi=[],
            conn=conn,
        )

        node_id = await self.graph_store.get_node_id(canonical_name=budo.canonical_name, node_type='protein', conn=conn)
        return node_id

    async def _upsert_domain_nodes(
        self, 
        conn: Connection, 
        budo: "BudoV3",
        protein_node_id: uuid.UUID,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Dict[str, uuid.UUID]:
        """
        Upsert domains as nodes + create protein-domain edges.
        
        Returns:
            dict: {domain_id: node_id} mapping
        """
        domain_node_ids = {}
        
        for domain in budo.domains:
            # Domain canonical name: PROTEIN_CANONICAL:DOMAIN_ID
            domain_canonical = f"{budo.canonical_name}:{domain.domain_id}"
            
            # Properties
            properties = {
                'budo_id': budo.budoId,
                'domain_name': domain.domain_name,
                'domain_type': domain.domain_type,
                'start_position': domain.start_position,
                'end_position': domain.end_position,
                'cath_id': domain.cath_id,
                'pfam_id': domain.pfam_id,
                'interpro_id': domain.interpro_id,
                'num_ptms': len(domain.ptms),
                'num_ligands': len(domain.ligands),
                'num_conformations': len(domain.conformations),
            }
            if user_id:
                properties['user_id'] = user_id
            if session_id:
                properties['session_id'] = session_id
            
            description = (
                f"{domain.domain_name} ({domain.domain_type}) in {budo.canonical_name}. "
                f"Range: {domain.start_position}-{domain.end_position}. "
                f"PTMs: {len(domain.ptms)}, Ligands: {len(domain.ligands)}."
            )
            
            await self.graph_store.upsert_node(
                canonical_name=domain_canonical,
                node_type='domain',
                aliases=[],
                description=description,
                embedding=None,
                external_ids={},
                properties=properties,
                source_doi=[],
                conn=conn,
            )

            domain_node_id = await self.graph_store.get_node_id(canonical_name=domain_canonical, node_type='domain', conn=conn)
            domain_node_ids[domain.domain_id] = domain_node_id
            
            # Create protein → domain edge
            await self._insert_edge(
                conn,
                source_node=budo.canonical_name,
                source_type='protein',
                target_node=domain_canonical,
                target_type='domain',
                relationship='contains_domain',
                details=f"{domain.domain_name} at positions {domain.start_position}-{domain.end_position}",
                confidence=_resolve_edge_confidence({'domain_type': domain.domain_type}, base=0.85),
                source_doi=None,
                metadata={'domain_type': domain.domain_type}
            )
        
        return domain_node_ids

    async def _insert_ptm_edges(
        self,
        conn: Connection,
        budo: "BudoV3",
        domain_node_ids: Dict[str, uuid.UUID],
        user_id: Optional[str],
        session_id: Optional[str]
    ) -> None:
        """
        Insert PTM edges: protein → phosphorylates/acetylates → protein.
        
        Architecture:
        - source_node: protein canonical name
        - relationship: 'phosphorylates', 'acetylates', 'methylates', 'ubiquitinates'
        - target_node: protein canonical name (self-modification)
        - details: "Position 185, residue Y in domain SH2_DOMAIN"
        """
        for domain in budo.domains:
            for ptm in domain.ptms:
                relationship = f"{ptm.ptm_type.lower()}s"  # phosphorylates, acetylates
                
                details = (
                    f"Position {ptm.position}, residue {ptm.residue} "
                    f"in domain {domain.domain_name}. "
                )
                if ptm.enzyme:
                    details += f"Enzyme: {ptm.enzyme}."
                
                metadata = {
                    'position': ptm.position,
                    'residue': ptm.residue,
                    'ptm_type': ptm.ptm_type,
                    'domain_id': domain.domain_id,
                    'enzyme': ptm.enzyme,
                }
                
                await self._insert_edge(
                    conn,
                    source_node=budo.canonical_name,
                    source_type='protein',
                    target_node=budo.canonical_name,  # Self-modification
                    target_type='protein',
                    relationship=relationship,
                    details=details,
                    confidence=_resolve_edge_confidence(metadata, base=0.7),
                    source_doi=None,
                    metadata=metadata,
                    user_id=user_id,
                    session_id=session_id
                )

    async def _insert_ligand_edges(
        self,
        conn: Connection,
        budo: "BudoV3",
        domain_node_ids: Dict[str, uuid.UUID],
        user_id: Optional[str],
        session_id: Optional[str]
    ) -> None:
        """
        Insert ligand binding edges: protein → binds_ligand → compound.
        
        Architecture:
        - source_node: protein canonical name
        - relationship: 'binds_ligand'
        - target_node: ChEMBL ID or PubChem ID
        - details: "Binding residues: 185,186,187 in domain SH2_DOMAIN"
        """
        for domain in budo.domains:
            for ligand in domain.ligands:
                # Target: prefer ChEMBL, fallback to PubChem
                target_node = ligand.chembl_id or f"PUBCHEM:{ligand.pubchem_id}"
                
                details = f"Binding residues: {ligand.binding_residues} in domain {domain.domain_name}."
                
                metadata = {
                    'chembl_id': ligand.chembl_id,
                    'pubchem_id': ligand.pubchem_id,
                    'binding_residues': ligand.binding_residues,
                    'domain_id': domain.domain_id,
                }
                
                await self._insert_edge(
                    conn,
                    source_node=budo.canonical_name,
                    source_type='protein',
                    target_node=target_node,
                    target_type='compound',
                    relationship='binds_ligand',
                    details=details,
                    confidence=_resolve_edge_confidence(metadata, base=0.75),
                    source_doi=None,
                    metadata=metadata,
                    user_id=user_id,
                    session_id=session_id
                )

    async def _insert_interface_edges(
        self,
        conn: Connection,
        budo: "BudoV3",
        protein_node_id: uuid.UUID,
        user_id: Optional[str],
        session_id: Optional[str]
    ) -> None:
        """
        Insert protein-protein interface edges.
        
        Architecture:
        - source_node: protein canonical name
        - relationship: 'interacts_with' or 'docks_with'
        - target_node: partner protein ID
        - details: "Interface residues: 150,151,155. HADDOCK score: -120.5"
        """
        for interface in budo.interfaces:
            relationship = 'docks_with' if interface.interface_type == 'docking' else 'interacts_with'
            
            residues_str = ','.join(map(str, interface.interface_residues))
            details = f"Interface residues: {residues_str}. Chain: {interface.partner_chain or 'A'}."
            
            if interface.haddock_score:
                details += f" HADDOCK score: {interface.haddock_score:.2f}."
            
            metadata = {
                'partner_protein': interface.partner_protein_id,
                'partner_chain': interface.partner_chain,
                'interface_residues': interface.interface_residues,
                'interface_type': interface.interface_type,
                'haddock_score': interface.haddock_score,
            }
            
            await self._insert_edge(
                conn,
                source_node=budo.canonical_name,
                source_type='protein',
                target_node=interface.partner_protein_id,
                target_type='protein',
                relationship=relationship,
                details=details,
                confidence=0.8 if interface.interface_type == 'predicted' else 1.0,
                source_doi=None,
                metadata=metadata,
                user_id=user_id,
                session_id=session_id
            )

    async def _insert_structural_facts(
        self,
        conn: Connection,
        budo: "BudoV3",
        protein_node_id: uuid.UUID,
        user_id: Optional[str],
        session_id: Optional[str]
    ) -> None:
        """
        Insert motifs and catalytic residues as atom_facts.
        
        Fact types:
        - Motifs: "DFG motif at positions 180-182 in ABL1_HUMAN"
        - Catalytic residues: "Catalytic residue at position 185 in ABL1_HUMAN"
        """
        for domain in budo.domains:
            # Motifs
            for motif in domain.motifs:
                positions_str = ','.join(map(str, motif.get('positions', [])))
                content = (
                    f"{motif.get('name', 'Unknown')} motif at positions {positions_str} "
                    f"in {budo.canonical_name} domain {domain.domain_name}."
                )
                
                await self._insert_fact(
                    conn,
                    content=content,
                    fact_type='structural_feature',
                    topic='protein_motif',
                    entities=[budo.canonical_name, motif.get('name', 'Unknown')],
                    confidence=_resolve_edge_confidence({'motif_name': motif.get('name')}, base=0.8),
                    user_id=user_id,
                    session_id=session_id
                )
            
            # Catalytic residues
            for position in domain.catalytic_residues:
                content = (
                    f"Catalytic residue at position {position} in {budo.canonical_name} "
                    f"domain {domain.domain_name}."
                )
                
                await self._insert_fact(
                    conn,
                    content=content,
                    fact_type='structural_feature',
                    topic='catalytic_site',
                    entities=[budo.canonical_name],
                    confidence=_resolve_edge_confidence({'position': position}, base=0.85),
                    user_id=user_id,
                    session_id=session_id
                )

    async def _insert_edge(
        self,
        conn: Connection,
        source_node: str,
        source_type: str,
        target_node: str,
        target_type: str,
        relationship: str,
        details: str,
        confidence: float,
        source_doi: Optional[str],
        metadata: Optional[Dict[str, Any]] = None,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None
    ) -> uuid.UUID:
        """
        Insert edge into atom_graph_edges (hypertable).
        """
        await self.graph_store.upsert_edge(
            source_node=source_node,
            source_type=source_type,
            target_node=target_node,
            target_type=target_type,
            relationship=relationship,
            details=details,
            confidence=confidence,
            source_doi=source_doi,
            metadata=metadata or {},
            user_id=user_id,
            session_id=session_id,
            conn=conn,
        )

        return None

    async def _insert_fact(
        self,
        conn: Connection,
        content: str,
        fact_type: str,
        topic: str,
        entities: List[str],
        confidence: float,
        source_doi: Optional[str] = None,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None
    ) -> uuid.UUID:
        """
        Insert fact into atom_facts (hypertable).
        """
        await self.graph_store.upsert_fact(
            content=content,
            fact_type=fact_type,
            topic=topic,
            entities=entities,
            confidence=confidence,
            source_doi=source_doi,
            user_id=user_id,
            session_id=session_id,
            metadata={"source": "budo"},
            conn=conn,
        )

        return None


# ============================================================================
# ASYNC CONTEXT MANAGER
# ============================================================================

class BudoGraphWriterFactory:
    """
    Factory for creating BudoGraphWriter instances with connection pooling.
    
    Usage:
        async with BudoGraphWriterFactory(dsn) as writer:
            await writer.upsert_budo_as_graph(budo)
    """
    
    def __init__(self, dsn: str, min_size: int = 5, max_size: int = 20):
        self.dsn = dsn
        self.min_size = min_size
        self.max_size = max_size
        self.pool: Optional[Pool] = None
    
    async def __aenter__(self) -> BudoGraphWriter:
        self.pool = await create_asyncpg_pool_for_database_url(
            self.dsn,
            min_size=self.min_size,
            max_size=self.max_size,
            command_timeout=30,
            timeout=20,
        )
        return BudoGraphWriter(self.pool)
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.pool:
            await self.pool.close()


# ============================================================================
# SYNCHRONOUS WRAPPER
# ============================================================================

def upsert_budo_sync(dsn: str, budo: "BudoV3") -> str:
    """
    Synchronous wrapper for upsert_budo_as_graph.
    
    Args:
        dsn: Postgres DSN (e.g., "postgres://user:pass@localhost/mica")
        budo: BudoV3 object
        
    Returns:
        node_id: UUID string of protein node
    """
    async def _run():
        async with BudoGraphWriterFactory(dsn) as writer:
            return await writer.upsert_budo_as_graph(budo)
    
    return asyncio.run(_run())
