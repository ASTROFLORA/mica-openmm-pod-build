# TC03 — Metadata Injection (incl. namespaces)

**Objetivo**
- Confirmar que `context_tags` se inyectan en los XML como `<Metadata><ScannerInfo><Tag ...>`.
- Confirmar que funciona incluso si el XML tiene namespaces.

**Setup**
- Se valida un XML real generado en TC01.
- Se prueba `scanner._inject_metadata()` sobre un XML con namespace `urn:lmp`.

**Expectativa**
- Existe nodo `ScannerInfo` y `Tag key=test` con valor `tc01`.
- En XML con namespace, la inyección también ocurre (búsqueda por local-name).

**Observado**
- Pasa.

**Post-mortem**
- La primera versión usaba `root.find("Metadata")` y fallaba con namespaces.

**Fix aplicado**
- Se implementó búsqueda por local-name y creación preservando el namespace del root.

**Notas**
- La serialización actual usa `ET.tostring` sin pretty print; es intencional para minimizar riesgo de romper estructura.
