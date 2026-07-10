"""
Human Kinase Catalog Generator
===============================

Fetches all human kinases from UniProt and creates comprehensive catalog
for batch HPC processing.

Author: Alex Rodriguez (Chief Data Architect)
Lab: Alex Rodriguez AI Systems Architecture Lab
Phase: 1.004 - UniProt Bootstrap Scale-Up
Date: October 8, 2025
Version: 1.0.0

Data Source: UniProt REST API
Query: reviewed:true AND organism_id:9606 AND keyword:KW-0418 (Kinase)
"""

import argparse
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlencode

import requests

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class KinaseCatalogGenerator:
    """
    Generate comprehensive catalog of human kinases from UniProt.
    """
    
    UNIPROT_API_BASE = "https://rest.uniprot.org/uniprotkb"
    
    # UniProt query for human kinases (reviewed only)
    KINASE_QUERY = "reviewed:true AND organism_id:9606 AND keyword:KW-0418"
    
    # Fields to retrieve from UniProt
    FIELDS = [
        "accession",          # UniProt ID
        "id",                 # Entry name (e.g., ABL1_HUMAN)
        "gene_primary",       # Primary gene name
        "gene_synonym",       # Gene synonyms
        "protein_name",       # Recommended protein name
        "organism_name",      # Organism
        "length",             # Sequence length
        "mass",               # Molecular weight
        "sequence",           # Amino acid sequence
        "cc_function",        # Function description
        "ft_act_site",        # Active site annotations
        "ft_binding",         # Binding site annotations
        "xref_pdb",           # PDB cross-references
        "xref_string",        # STRING database
        "xref_refseq",        # RefSeq
        "xref_ensembl",       # Ensembl
        "go_p",               # GO Biological Process
        "go_c",               # GO Cellular Component
        "go_f",               # GO Molecular Function
        "ec",                 # EC number
    ]
    
    def __init__(
        self,
        rate_limit_delay: float = 0.5,
        max_retries: int = 3
    ):
        """
        Initialize catalog generator.
        
        Args:
            rate_limit_delay: Delay between API requests (seconds)
            max_retries: Maximum retry attempts for failed requests
        """
        self.rate_limit_delay = rate_limit_delay
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'BSM-BUDO-CEA/1.0 (alex.rodriguez@bsm.org)'
        })
    
    def fetch_kinases(self, batch_size: int = 500) -> List[Dict]:
        """
        Fetch all human kinases from UniProt.
        
        Args:
            batch_size: Number of results per API request
            
        Returns:
            List of kinase metadata dictionaries
        """
        logger.info(f"Fetching human kinases from UniProt (query: {self.KINASE_QUERY})")
        
        kinases = []
        cursor = None
        page = 1
        
        while True:
            params = {
                'query': self.KINASE_QUERY,
                'format': 'json',
                'fields': ','.join(self.FIELDS),
                'size': batch_size
            }
            
            if cursor:
                params['cursor'] = cursor
            
            url = f"{self.UNIPROT_API_BASE}/search?{urlencode(params)}"
            
            logger.info(f"Fetching page {page}...")
            
            response = self._make_request(url)
            
            if response is None:
                logger.error(f"Failed to fetch page {page} after {self.max_retries} retries")
                break
            
            data = response.json()
            results = data.get('results', [])
            
            if not results:
                logger.info("No more results")
                break
            
            # Process results
            for entry in results:
                kinase = self._parse_uniprot_entry(entry)
                kinases.append(kinase)
            
            logger.info(f"Page {page}: {len(results)} kinases (total: {len(kinases)})")
            
            # Check for next page
            cursor = data.get('nextCursor')
            if not cursor:
                break
            
            page += 1
            time.sleep(self.rate_limit_delay)
        
        logger.info(f"Fetched {len(kinases)} human kinases")
        return kinases
    
    def _make_request(self, url: str) -> Optional[requests.Response]:
        """
        Make HTTP request with retry logic.
        
        Args:
            url: Request URL
            
        Returns:
            Response object or None if failed
        """
        for attempt in range(self.max_retries):
            try:
                response = self.session.get(url, timeout=30)
                response.raise_for_status()
                return response
                
            except requests.exceptions.RequestException as e:
                logger.warning(f"Request failed (attempt {attempt + 1}/{self.max_retries}): {e}")
                
                if attempt < self.max_retries - 1:
                    time.sleep(2 ** attempt)  # Exponential backoff
                else:
                    return None
    
    def _parse_uniprot_entry(self, entry: Dict) -> Dict:
        """
        Parse UniProt entry into kinase metadata.
        
        Args:
            entry: UniProt API response entry
            
        Returns:
            Kinase metadata dictionary
        """
        # Extract primary accession
        uniprot_id = entry.get('primaryAccession', '')
        
        # Extract gene information
        genes = entry.get('genes', [])
        gene_primary = None
        gene_synonyms = []
        
        if genes:
            gene_primary = genes[0].get('geneName', {}).get('value')
            synonyms = genes[0].get('synonyms', [])
            gene_synonyms = [s.get('value') for s in synonyms if s.get('value')]
        
        # Extract protein names
        protein_description = entry.get('proteinDescription', {})
        recommended_name = protein_description.get('recommendedName', {})
        protein_name = recommended_name.get('fullName', {}).get('value', '')
        
        # Extract organism
        organism = entry.get('organism', {})
        organism_name = organism.get('scientificName', 'Homo sapiens')
        taxonomy_id = organism.get('taxonId', 9606)
        
        # Extract sequence info
        sequence = entry.get('sequence', {})
        sequence_str = sequence.get('value', '')
        sequence_length = sequence.get('length', 0)
        molecular_weight = sequence.get('molWeight', 0)
        
        # Extract cross-references
        xrefs = entry.get('uniProtKBCrossReferences', [])
        
        pdb_ids = [
            xref.get('id') 
            for xref in xrefs 
            if xref.get('database') == 'PDB'
        ]
        
        string_ids = [
            xref.get('id') 
            for xref in xrefs 
            if xref.get('database') == 'STRING'
        ]
        
        refseq_ids = [
            xref.get('id') 
            for xref in xrefs 
            if xref.get('database') == 'RefSeq'
        ]
        
        ensembl_ids = [
            xref.get('id') 
            for xref in xrefs 
            if xref.get('database') == 'Ensembl'
        ]
        
        # Extract GO terms
        go_annotations = entry.get('uniProtKBCrossReferences', [])
        go_terms = {
            'biological_process': [],
            'cellular_component': [],
            'molecular_function': []
        }
        
        for xref in go_annotations:
            if xref.get('database') == 'GO':
                go_id = xref.get('id')
                properties = xref.get('properties', [])
                
                for prop in properties:
                    if prop.get('key') == 'GoTerm':
                        term_type = prop.get('value', '').split(':')[0]
                        
                        if term_type == 'P':
                            go_terms['biological_process'].append(go_id)
                        elif term_type == 'C':
                            go_terms['cellular_component'].append(go_id)
                        elif term_type == 'F':
                            go_terms['molecular_function'].append(go_id)
        
        # Extract EC numbers
        ec_numbers = [
            xref.get('id') 
            for xref in entry.get('uniProtKBCrossReferences', [])
            if xref.get('database') == 'EC'
        ]
        
        # Build kinase metadata
        kinase = {
            'uniprot_id': uniprot_id,
            'entry_name': entry.get('uniProtkbId', ''),
            'gene_symbol': gene_primary,
            'gene_synonyms': gene_synonyms,
            'protein_name': protein_name,
            'organism': organism_name,
            'taxonomy_id': taxonomy_id,
            'sequence': sequence_str,
            'sequence_length': sequence_length,
            'molecular_weight': molecular_weight,
            'cross_references': {
                'pdb': pdb_ids,
                'string': string_ids,
                'refseq': refseq_ids,
                'ensembl': ensembl_ids
            },
            'go_terms': go_terms,
            'ec_numbers': ec_numbers,
            'fetched_at': datetime.utcnow().isoformat()
        }
        
        return kinase
    
    def save_catalog(self, kinases: List[Dict], output_path: Path) -> None:
        """
        Save kinase catalog to JSON file.
        
        Args:
            kinases: List of kinase metadata
            output_path: Output file path
        """
        logger.info(f"Saving catalog to {output_path}")
        
        catalog = {
            'metadata': {
                'total_kinases': len(kinases),
                'organism': 'Homo sapiens (human)',
                'taxonomy_id': 9606,
                'source': 'UniProt',
                'query': self.KINASE_QUERY,
                'generated_at': datetime.utcnow().isoformat(),
                'version': '1.0.0'
            },
            'kinases': kinases
        }
        
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(output_path, 'w') as f:
            json.dump(catalog, f, indent=2)
        
        logger.info(f"✓ Saved {len(kinases)} kinases to {output_path}")
    
    def generate_summary(self, kinases: List[Dict]) -> Dict:
        """
        Generate summary statistics for kinase catalog.
        
        Args:
            kinases: List of kinase metadata
            
        Returns:
            Summary statistics dictionary
        """
        summary = {
            'total_kinases': len(kinases),
            'with_pdb_structures': sum(1 for k in kinases if k['cross_references']['pdb']),
            'with_string_network': sum(1 for k in kinases if k['cross_references']['string']),
            'with_refseq': sum(1 for k in kinases if k['cross_references']['refseq']),
            'with_ensembl': sum(1 for k in kinases if k['cross_references']['ensembl']),
            'avg_sequence_length': sum(k['sequence_length'] for k in kinases) / len(kinases) if kinases else 0,
            'avg_molecular_weight': sum(k['molecular_weight'] for k in kinases) / len(kinases) if kinases else 0,
        }
        
        return summary


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description='Generate Human Kinase Catalog from UniProt')
    
    parser.add_argument(
        '--output',
        type=Path,
        default=Path('data/kinase_catalog_human.json'),
        help='Output catalog file path'
    )
    
    parser.add_argument(
        '--batch-size',
        type=int,
        default=500,
        help='Number of results per API request'
    )
    
    parser.add_argument(
        '--rate-limit',
        type=float,
        default=0.5,
        help='Delay between API requests (seconds)'
    )
    
    args = parser.parse_args()
    
    # Generate catalog
    generator = KinaseCatalogGenerator(rate_limit_delay=args.rate_limit)
    kinases = generator.fetch_kinases(batch_size=args.batch_size)
    
    # Save catalog
    generator.save_catalog(kinases, args.output)
    
    # Print summary
    summary = generator.generate_summary(kinases)
    print("\n=== Kinase Catalog Summary ===")
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
