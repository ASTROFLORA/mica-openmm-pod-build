from __future__ import annotations
import hashlib
from typing import List, Dict, Any, Optional
from datetime import datetime
from mica.context_steward.contracts import DoctrineRef, DoctrineStatus

class DoctrineRegistryClient:
    def __init__(self):
        # Local registry store in memory
        self._store: Dict[str, DoctrineRef] = {}

    def register_doctrine(self, ref: DoctrineRef):
        self._store[ref.doctrine_ref] = ref

    def resolve(self, domain: str, scope: str) -> Optional[DoctrineRef]:
        # Filter active or under_appeal doctrine refs for this domain and scope
        candidates = [
            r for r in self._store.values()
            if r.domain == domain and r.scope == scope and r.status in (DoctrineStatus.ACTIVE, DoctrineStatus.UNDER_APPEAL)
        ]
        if not candidates:
            return None
        # Sort by version descending to get latest
        candidates.sort(key=lambda x: x.version, reverse=True)
        return candidates[0]

    def get(self, doctrine_ref: str) -> Optional[DoctrineRef]:
        return self._store.get(doctrine_ref)
