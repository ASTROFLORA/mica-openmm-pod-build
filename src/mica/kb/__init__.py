"""KB module — Knowledge Base domain objects for scientific claims and evidence.

Implements the KB doctrine from PROYECTO_TOLOMEO (K0.1–K7):
- ClaimAtom: atomic scientific claim with entity binding, context, quantification
- EvidenceItem: typed evidence with kind discriminator and support direction
- SemanticContextRegistry: deduplicable, versionable biological context
- ClaimAtomBridge: connects DLM extraction to KB domain
"""
