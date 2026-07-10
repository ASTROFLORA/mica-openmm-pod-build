"""Canonical Entity Atlas service layer.

Provides CRUD operations, identifier resolution, and graph synchronization
against Neo4j for the CEA identity registry.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterable, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - type checking support
    from neo4j import AsyncSession
else:  # pragma: no cover - runtime fallback when neo4j is absent
    AsyncSession = Any

from ..neo4j_integration import BSMNeo4jIntegration
from ..neo4j_integration import Neo4jError  # type: ignore
from .exceptions import CEADuplicateError, CEAError, CEANotFoundError
from .id_generator import BudoIdGenerator
from ..schemas.cea import (
    AuditTrail,
    CEAEntity,
    CompositeIdentifiers,
    ExternalReferences,
    LigandAssociation,
    VariantAnnotation,
)

logger = logging.getLogger(__name__)


class CEAService:
    """High-level interface to manage CEA entities."""

    def __init__(
        self,
        neo4j: BSMNeo4jIntegration,
        *,
        id_generator: Optional[BudoIdGenerator] = None,
        namespace: str = "budo",
    ) -> None:
        self.neo4j = neo4j
        self.id_generator = id_generator or BudoIdGenerator(namespace=namespace)

    async def create_entity(self, entity: CEAEntity) -> CEAEntity:
        """Create a new CEA entity in Neo4j.

        Raises:
            CEADuplicateError: if the budo_id already exists
        """
        async with self.neo4j.session() as session:
            if await self._exists(session, entity.budo_id):
                raise CEADuplicateError(f"Entity {entity.budo_id} already exists")
            node = await self._persist_entity(session, entity, create=True)
            await self._sync_relationships(session, entity)
            return self._node_to_entity(node)

    async def update_entity(self, entity: CEAEntity) -> CEAEntity:
        """Update an existing CEA entity."""
        async with self.neo4j.session() as session:
            if not await self._exists(session, entity.budo_id):
                raise CEANotFoundError(f"Entity {entity.budo_id} not found")
            node = await self._persist_entity(session, entity, create=False)
            await self._sync_relationships(session, entity)
            return self._node_to_entity(node)

    async def resolve(self, identifier: str) -> CEAEntity:
        """Resolve an entity from any known identifier."""
        async with self.neo4j.session() as session:
            node = await self._fetch_by_identifier(session, identifier)
            if not node:
                raise CEANotFoundError(f"Identifier '{identifier}' is unknown to CEA")
            return self._node_to_entity(node)

    async def exists(self, budo_id: str) -> bool:
        """Return whether a CEA entity exists for a root BUDO identifier."""
        async with self.neo4j.session() as session:
            return await self._exists(session, budo_id)

    async def get_by_external_id(self, identifier: str) -> Optional[CEAEntity]:
        """Return an entity by an external identifier, or ``None`` if absent."""
        try:
            return await self.resolve(identifier)
        except CEANotFoundError:
            return None

    async def delete(self, budo_id: str) -> None:
        """Remove an entity and all associated relationships."""
        async with self.neo4j.session() as session:
            await session.run(
                """
                MATCH (e:CEA {budo_id: $budo_id})
                OPTIONAL MATCH (e)-[r]-()
                DELETE r, e
                """,
                {"budo_id": budo_id},
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _exists(self, session: AsyncSession, budo_id: str) -> bool:
        result = await session.run(
            "MATCH (e:CEA {budo_id: $budo_id}) RETURN e.budo_id AS bid",
            {"budo_id": budo_id},
        )
        return (await result.single()) is not None

    async def _persist_entity(
        self,
        session: AsyncSession,
        entity: CEAEntity,
        *,
        create: bool,
    ) -> Dict[str, Any]:
        payload = self._entity_to_payload(entity)
        if create:
            query = (
                """
                CREATE (e:CEA {
                    budo_id: $budo_id,
                    entity_type: $entity_type,
                    name: $name,
                    organism: $organism,
                    version: $version,
                    description: $description,
                    tags: $tags,
                    search_tokens: $search_tokens,
                    composite_ids_json: $composite_ids_json,
                    cross_references_json: $cross_references_json,
                    ligands_json: $ligands_json,
                    variants_json: $variants_json,
                    metadata_json: $metadata_json,
                    audit_json: $audit_json,
                    keyword_tokens: $keyword_tokens,
                    created_at: datetime(),
                    updated_at: datetime()
                })
                RETURN e
                """
            )
        else:
            query = (
                """
                MATCH (e:CEA {budo_id: $budo_id})
                SET e.entity_type = $entity_type,
                    e.name = $name,
                    e.organism = $organism,
                    e.version = $version,
                    e.description = $description,
                    e.tags = $tags,
                    e.search_tokens = $search_tokens,
                    e.composite_ids_json = $composite_ids_json,
                    e.cross_references_json = $cross_references_json,
                    e.ligands_json = $ligands_json,
                    e.variants_json = $variants_json,
                    e.metadata_json = $metadata_json,
                    e.audit_json = $audit_json,
                    e.keyword_tokens = $keyword_tokens,
                    e.updated_at = datetime()
                RETURN e
                """
            )

        try:
            result = await session.run(query, payload)
            record = await result.single()
        except Neo4jError as exc:  # pragma: no cover - driver specific
            logger.error("Neo4j error persisting CEA entity %s: %s", entity.budo_id, exc)
            raise CEAError(str(exc)) from exc

        if not record:
            raise CEAError("Failed to persist CEA entity")
        node = record["e"]
        return dict(node)

    async def _sync_relationships(self, session: AsyncSession, entity: CEAEntity) -> None:
        await self._sync_ligands(session, entity)
        await self._sync_cross_references(session, entity)

    async def _sync_ligands(self, session: AsyncSession, entity: CEAEntity) -> None:
        await session.run(
            """
            MATCH (e:CEA {budo_id: $budo_id})-[r:BINDS_TO]->(l:Ligand)
            DELETE r
            """,
            {"budo_id": entity.budo_id},
        )

        for ligand in entity.ligands:
            params = {
                "budo_id": entity.budo_id,
                "ligand_id": ligand.ligand_id,
                "name": ligand.name,
                "source": ligand.source,
                "relationship": ligand.relationship,
                "affinity": ligand.affinity_nm,
                "smiles": ligand.smiles,
                "inchi_key": ligand.inchi_key,
                "evidence": ligand.evidence,
                "references": ligand.references,
            }
            await session.run(
                """
                MERGE (l:Ligand {ligand_id: $ligand_id})
                SET l.name = $name,
                    l.source = $source,
                    l.smiles = $smiles,
                    l.inchi_key = $inchi_key
                WITH l
                MATCH (e:CEA {budo_id: $budo_id})
                MERGE (e)-[r:BINDS_TO]->(l)
                SET r.relationship = $relationship,
                    r.affinity_nm = $affinity,
                    r.evidence = $evidence,
                    r.references = $references
                """,
                params,
            )

    async def _sync_cross_references(self, session: AsyncSession, entity: CEAEntity) -> None:
        await session.run(
            """
            MATCH (e:CEA {budo_id: $budo_id})<-[r:IDENTIFIES]-(cr:CrossReference)
            DELETE r
            """,
            {"budo_id": entity.budo_id},
        )

        for system, identifier in self._iterate_cross_references(entity.cross_references):
            params = {
                "budo_id": entity.budo_id,
                "system": system,
                "identifier": identifier,
            }
            await session.run(
                """
                MERGE (cr:CrossReference {system: $system, identifier: $identifier})
                WITH cr
                MATCH (e:CEA {budo_id: $budo_id})
                MERGE (cr)-[r:IDENTIFIES]->(e)
                SET r.system = $system,
                    r.identifier = $identifier
                """,
                params,
            )

    async def _fetch_by_identifier(self, session: AsyncSession, identifier: str) -> Optional[Dict[str, Any]]:
        normalized = identifier.lower()
        result = await session.run(
            """
            MATCH (e:CEA)
            WHERE e.budo_id = $identifier OR $normalized IN e.search_tokens
            RETURN e
            LIMIT 1
            """,
            {"identifier": identifier, "normalized": normalized},
        )
        record = await result.single()
        return dict(record["e"]) if record else None

    def _entity_to_payload(self, entity: CEAEntity) -> Dict[str, Any]:
        entity_json = entity.model_dump(mode="json")

        composite_ids_json = json.dumps(entity_json["composite_ids"])
        cross_references_json = json.dumps(entity_json["cross_references"])
        ligands_json = json.dumps(entity_json["ligands"])
        variants_json = json.dumps(entity_json["variants"])
        metadata_json = json.dumps(entity_json["metadata"])
        audit_json = json.dumps(entity_json["audit"])

        search_tokens = self._build_search_tokens(entity)

        payload = {
            "budo_id": entity_json["budo_id"],
            "entity_type": entity_json["entity_type"],
            "name": entity_json["name"],
            "organism": entity_json.get("organism"),
            "version": entity_json.get("version"),
            "description": entity_json.get("description"),
            "tags": entity_json.get("tags", []),
            "keyword_tokens": entity.keyword_tokens(),
            "search_tokens": search_tokens,
            "composite_ids_json": composite_ids_json,
            "cross_references_json": cross_references_json,
            "ligands_json": ligands_json,
            "variants_json": variants_json,
            "metadata_json": metadata_json,
            "audit_json": audit_json,
        }
        return payload

    def _build_search_tokens(self, entity: CEAEntity) -> List[str]:
        tokens = set(token.lower() for token in entity.keyword_tokens())
        tokens.add(entity.budo_id.lower())
        tokens.add(entity.name.lower())
        if entity.organism:
            tokens.add(entity.organism.lower())

        composite = entity.composite_ids.model_dump()
        for value in composite.values():
            if value:
                tokens.add(str(value).lower())

        cross_refs = entity.cross_references.model_dump()
        for value in cross_refs.values():
            if isinstance(value, list):
                tokens.update(str(item).lower() for item in value)
            elif value:
                tokens.add(str(value).lower())

        for ligand in entity.ligands:
            tokens.add(ligand.ligand_id.lower())
            tokens.add(ligand.name.lower())
        return sorted(tokens)

    def _node_to_entity(self, node: Dict[str, Any]) -> CEAEntity:
        composite_ids = json.loads(node.get("composite_ids_json", "{}"))
        cross_refs = json.loads(node.get("cross_references_json", "{}"))
        ligands = json.loads(node.get("ligands_json", "[]"))
        variants = json.loads(node.get("variants_json", "[]"))
        metadata = json.loads(node.get("metadata_json", "{}"))
        audit = json.loads(node.get("audit_json", "{}"))

        return CEAEntity(
            budo_id=node["budo_id"],
            entity_type=node.get("entity_type", "Protein"),
            name=node.get("name", ""),
            organism=node.get("organism"),
            version=str(node.get("version", "1.0")),
            description=node.get("description"),
            tags=list(node.get("tags", [])),
            composite_ids=CompositeIdentifiers(**composite_ids),
            cross_references=ExternalReferences(**cross_refs),
            ligands=[LigandAssociation(**l) for l in ligands],
            variants=[VariantAnnotation(**v) for v in variants],
            metadata=metadata,
            audit=AuditTrail(**audit) if audit else AuditTrail(),
        )

    async def _fetch_raw_by_budo_id(self, session: AsyncSession, budo_id: str) -> Optional[Dict[str, Any]]:
        result = await session.run(
            "MATCH (e:CEA {budo_id: $budo_id}) RETURN e",
            {"budo_id": budo_id},
        )
        record = await result.single()
        return dict(record["e"]) if record else None

    @staticmethod
    def _iterate_cross_references(cross_refs: ExternalReferences) -> Iterable[tuple[str, str]]:
        data = cross_refs.model_dump()
        for system, value in data.items():
            if value is None:
                continue
            if isinstance(value, list):
                for item in value:
                    yield system, str(item)
            else:
                yield system, str(value)
