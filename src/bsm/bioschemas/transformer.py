"""
BioSchemas JSON-LD Transformer

Converts BUDO V3 objects to BioSchemas-compliant JSON-LD for semantic 
interoperability with external knowledge graphs.

Reference: https://bioschemas.org/profiles/Protein/0.11-RELEASE

Created: October 8, 2025
Author: Alex Rodriguez
"""

from typing import Dict, List, Optional
import json
from pathlib import Path
from datetime import datetime

try:
    from bsm.schemas import BudoV3, BioSiteV3, DomainV3, VariantV3
except ImportError:
    # Fallback for testing
    BudoV3 = BioSiteV3 = DomainV3 = VariantV3 = object


class BioSchemasTransformer:
    """
    Transform BUDO objects to BioSchemas JSON-LD
    
    Supports:
    - Schema.org Protein profile
    - BioChemEntity for BioSites
    - SequenceAnnotation for Domains
    - External cross-references (UniProt, PDB, Ensembl)
    """
    
    CONTEXT = "http://schema.org/"
    BASE_URL = "https://bsm.example.org"  # TODO: Replace with actual production URL
    
    # Taxonomy ID mapping (expand as needed)
    TAXON_MAP = {
        "Homo sapiens": "http://purl.uniprot.org/taxonomy/9606",
        "Mus musculus": "http://purl.uniprot.org/taxonomy/10090",
        "Rattus norvegicus": "http://purl.uniprot.org/taxonomy/10116",
        "Danio rerio": "http://purl.uniprot.org/taxonomy/7955",
        "Drosophila melanogaster": "http://purl.uniprot.org/taxonomy/7227"
    }
    
    def __init__(self, base_url: Optional[str] = None):
        """
        Initialize BioSchemas transformer
        
        Args:
            base_url: Custom base URL for @id generation (defaults to class BASE_URL)
        """
        if base_url:
            self.base_url = base_url
        else:
            self.base_url = self.BASE_URL
    
    def budo_to_jsonld(self, budo: BudoV3) -> Dict:
        """
        Transform BUDO V3 to BioSchemas Protein JSON-LD
        
        Args:
            budo: BudoV3 Pydantic model
        
        Returns:
            JSON-LD dictionary (BioSchemas Protein profile)
        
        Example:
            >>> transformer = BioSchemasTransformer()
            >>> budo = BudoV3(budo_id="budo:WNK1-S-001", ...)
            >>> jsonld = transformer.budo_to_jsonld(budo)
            >>> print(jsonld["@type"])
            'Protein'
        """
        jsonld = {
            "@context": self.CONTEXT,
            "@type": "Protein",
            "@id": f"{self.base_url}/budo/{budo.budo_id}",
            "identifier": budo.budo_id,
            "name": budo.name,
            "alternateName": budo.gene_symbol,
            "url": f"{self.base_url}/proteins/{budo.gene_symbol}"
        }
        
        # Add organism/taxonomy
        if budo.organism:
            jsonld["taxonomicRange"] = self._create_taxon(budo.organism)
        
        # Add sequence annotations (domains)
        if hasattr(budo, 'domains') and budo.domains:
            jsonld["hasSequenceAnnotation"] = self._create_sequence_annotations(budo.domains)
        
        # Add BioSites as BioChemEntityPart
        if hasattr(budo, 'biosites') and budo.biosites:
            jsonld["hasBioChemEntityPart"] = self._create_biochem_entities(budo.biosites)
        
        # Add cross-references (sameAs)
        if budo.canonical_id:
            jsonld["sameAs"] = self._create_cross_references(budo)
        
        # Add gene encoding
        if budo.gene_symbol:
            jsonld["isEncodedByBioChemEntity"] = self._create_gene(budo.gene_symbol)
        
        # Add sequence properties
        if budo.sequence:
            jsonld["hasSequence"] = {
                "@type": "BioChemEntity",
                "identifier": f"{budo.budo_id}_sequence",
                "value": budo.sequence[:50] + "..." if len(budo.sequence) > 50 else budo.sequence,  # Truncate for readability
                "length": len(budo.sequence)
            }
        
        # Add functional state (custom property)
        if hasattr(budo, 'functional_state') and budo.functional_state:
            jsonld["additionalProperty"] = [
                {
                    "@type": "PropertyValue",
                    "name": "functionalState",
                    "value": budo.functional_state
                }
            ]
        
        return jsonld
    
    def biosite_to_jsonld(self, biosite: BioSiteV3, budo_id: str) -> Dict:
        """
        Transform BioSite V3 to BioSchemas BioChemEntity JSON-LD
        
        Args:
            biosite: BioSiteV3 Pydantic model
            budo_id: Parent BUDO ID for context
        
        Returns:
            JSON-LD dictionary (BioChemEntity)
        """
        jsonld = {
            "@context": self.CONTEXT,
            "@type": "BioChemEntity",
            "@id": f"{self.base_url}/biosite/{biosite.biosite_id}",
            "identifier": biosite.biosite_id,
            "name": f"{biosite.site_type} site",
            "isPartOf": {
                "@type": "Protein",
                "@id": f"{self.base_url}/budo/{budo_id}"
            }
        }
        
        # Add functional role
        if hasattr(biosite, 'functional_role') and biosite.functional_role:
            jsonld["molecularFunction"] = biosite.functional_role
        
        # Add residue positions
        if hasattr(biosite, 'residues') and biosite.residues:
            jsonld["sequenceLocation"] = {
                "@type": "SequenceRange",
                "positions": biosite.residues
            }
        
        # Add conformational state
        if hasattr(biosite, 'conformational_state') and biosite.conformational_state:
            jsonld["additionalProperty"] = [
                {
                    "@type": "PropertyValue",
                    "name": "conformationalState",
                    "value": biosite.conformational_state
                }
            ]
        
        # Add ligands
        if hasattr(biosite, 'ligands') and biosite.ligands:
            jsonld["chemicalRole"] = biosite.ligands
        
        return jsonld
    
    def _create_taxon(self, organism: str) -> Dict:
        """
        Create Schema.org Taxon object
        
        Args:
            organism: Organism name (e.g., "Homo sapiens")
        
        Returns:
            Taxon JSON-LD object
        """
        taxon = {
            "@type": "Taxon",
            "name": organism,
            "taxonRank": "species"
        }
        
        # Add NCBI Taxonomy URI if known
        if organism in self.TAXON_MAP:
            taxon["sameAs"] = self.TAXON_MAP[organism]
        
        return taxon
    
    def _create_sequence_annotations(self, domains: List[DomainV3]) -> List[Dict]:
        """
        Create SequenceAnnotation objects for domains
        
        Args:
            domains: List of DomainV3 objects
        
        Returns:
            List of SequenceAnnotation JSON-LD objects
        """
        annotations = []
        
        for domain in domains:
            annotation = {
                "@type": "SequenceAnnotation",
                "annotationType": "Domain",
                "sequenceLocation": {
                    "@type": "SequenceRange",
                    "rangeStart": domain.start,
                    "rangeEnd": domain.end
                }
            }
            
            # Add Pfam identifier if available
            if hasattr(domain, 'pfam_id') and domain.pfam_id:
                annotation["additionalProperty"] = {
                    "@type": "PropertyValue",
                    "name": "Pfam",
                    "value": domain.pfam_id
                }
            
            # Add domain type
            if hasattr(domain, 'domain_type') and domain.domain_type:
                annotation["description"] = f"{domain.domain_type} domain"
            
            annotations.append(annotation)
        
        return annotations
    
    def _create_biochem_entities(self, biosites: List[BioSiteV3]) -> List[Dict]:
        """
        Create BioChemEntity objects for BioSites
        
        Args:
            biosites: List of BioSiteV3 objects
        
        Returns:
            List of BioChemEntity JSON-LD objects
        """
        entities = []
        
        for biosite in biosites:
            entity = {
                "@type": "BioChemEntity",
                "@id": f"{self.base_url}/biosite/{biosite.biosite_id}",
                "name": f"{biosite.site_type} site"
            }
            
            if hasattr(biosite, 'functional_role') and biosite.functional_role:
                entity["molecularFunction"] = biosite.functional_role
            
            entities.append(entity)
        
        return entities
    
    def _create_cross_references(self, budo: BudoV3) -> List[str]:
        """
        Create sameAs cross-references to external databases
        
        Args:
            budo: BudoV3 object
        
        Returns:
            List of URIs to external resources
        """
        refs = []
        
        # UniProt reference (always include if canonical_id exists)
        if budo.canonical_id:
            refs.append(f"http://purl.uniprot.org/uniprot/{budo.canonical_id}")
        
        # Add additional cross-references if available
        if hasattr(budo, 'cross_references'):
            for xref in budo.cross_references:
                if hasattr(xref, 'database') and hasattr(xref, 'db_id'):
                    if xref.database == "PDB":
                        refs.append(f"https://www.rcsb.org/structure/{xref.db_id}")
                    elif xref.database == "Ensembl":
                        refs.append(f"http://www.ensembl.org/id/{xref.db_id}")
                    elif xref.database == "NCBI":
                        refs.append(f"https://www.ncbi.nlm.nih.gov/gene/{xref.db_id}")
        
        return refs
    
    def _create_gene(self, gene_symbol: str) -> Dict:
        """
        Create Schema.org Gene object
        
        Args:
            gene_symbol: Gene symbol (e.g., "WNK1")
        
        Returns:
            Gene JSON-LD object
        """
        return {
            "@type": "Gene",
            "identifier": gene_symbol,
            # TODO: Add HGNC or other gene database links
            # "sameAs": f"https://www.genenames.org/data/gene-symbol-report/#!/symbol/{gene_symbol}"
        }
    
    def export_knowledge_graph(self, budos: List[BudoV3], output_path: Path) -> None:
        """
        Export multiple BUDOs as JSON-LD knowledge graph
        
        Args:
            budos: List of BudoV3 objects
            output_path: Path to save JSON-LD file
        
        Example:
            >>> transformer = BioSchemasTransformer()
            >>> budos = [budo1, budo2, budo3]
            >>> transformer.export_knowledge_graph(budos, Path("knowledge_graph.jsonld"))
        """
        graph = {
            "@context": self.CONTEXT,
            "@graph": []
        }
        
        for budo in budos:
            jsonld = self.budo_to_jsonld(budo)
            graph["@graph"].append(jsonld)
        
        # Save to file
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(graph, f, indent=2, ensure_ascii=False)
        
        print(f"[BioSchemas] Exported {len(budos)} BUDOs to {output_path}")
    
    def validate_jsonld(self, jsonld: Dict) -> bool:
        """
        Validate JSON-LD structure (basic validation)
        
        Args:
            jsonld: JSON-LD dictionary
        
        Returns:
            True if valid, False otherwise
        
        TODO: Implement full Schema.org validation using pyshacl or similar
        """
        required_fields = ["@context", "@type", "@id", "identifier", "name"]
        
        for field in required_fields:
            if field not in jsonld:
                print(f"[BioSchemas] Validation failed: Missing required field '{field}'")
                return False
        
        return True


# Example usage
if __name__ == "__main__":
    # Example BUDO object (placeholder)
    class ExampleBudo:
        budo_id = "budo:WNK1-S-001"
        canonical_id = "P51617"
        gene_symbol = "WNK1"
        name = "Serine/threonine-protein kinase WNK1"
        organism = "Homo sapiens"
        sequence = "MAAAGAG" * 100  # Truncated example
        functional_state = "Active"
        domains = []
        biosites = []
    
    transformer = BioSchemasTransformer()
    jsonld = transformer.budo_to_jsonld(ExampleBudo())
    
    print(json.dumps(jsonld, indent=2))
    print(f"\nValidation: {transformer.validate_jsonld(jsonld)}")
