# LMP Generator — Config Reference (lmp_config.yaml)

Este documento lista los keys de configuración relevantes para el generador, tal como están definidos en `src/bsm/lmp/lmp_config.yaml`.

---

## generator

```yaml
generator:
  # API endpoints
  uniprot_api: "https://rest.uniprot.org/uniprotkb"
  pdb_api: "https://data.rcsb.org/rest/v1/core/entry"
  phosphosite_api: null

  # PubChem enrichment (optional; direct REST calls, no MCP)
  pubchem:
    enabled: false
    api_base: "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
    timeout_seconds: 10
    rate_limit: 0.25
    max_ligands_per_pdb: 20
    include_synonyms: false
    property_fields:
      - CanonicalSMILES
      - IsomericSMILES
      - InChI
      - InChIKey
      - MolecularFormula
      - MolecularWeight
      - IUPACName
    write_sidecar_json: true
    sidecar_prefix: "pubchem_enrichment"
    xml_include_pubchem_cid: false

  # Rate limiting (seconds between requests)
  rate_limit: 0.5

  # Caching
  cache_enabled: true
  cache_dir: "lmp_cache"
  cache_ttl: 86400

  # Default states for M-CSA proteins
  mcsa_default_states:
    - Apo_Inactive
    - Substrate_bound_Active

  # State inference
  infer_states_from_ptms: true
  infer_states_from_pdb: true
```

---

## Notas de compatibilidad (importantes)

1) `generator.cache_ttl` existe en YAML (segundos), pero el TTL efectivo de caché del generador puede estar controlado por constantes en código (dependiendo de la versión exacta de `generator.py`).

2) `generator.pubchem.xml_include_pubchem_cid: true` añade el atributo `pubchem_cid` a `<Ligand>` en el XML. Esto puede romper validación estricta si tu XSD no permite ese atributo.

Recomendación práctica:
- Mantén `xml_include_pubchem_cid: false` y usa `write_sidecar_json: true` para no tocar schema.

