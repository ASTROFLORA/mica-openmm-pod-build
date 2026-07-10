"""
PDB-based LMP XML Generator
============================
Generate LMP XMLs directly from PDB structures with actual PDB sequences.

This module extends the base LMPGenerator to support PDB-native workflows
where the exact crystallized sequences are needed (e.g., for CALVADOS3 simulations).

Author: AI University - LMP Team
Date: 2025-11-15
"""

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import xml.etree.ElementTree as ET
from xml.dom import minidom


class PDBLMPGenerator:
    """
    Generator for PDB-based LMP XML documents
    
    Key Features:
    - Fetches real PDB sequences (not UniProt)
    - Supports multi-chain structures
    - Includes experimental metadata (method, resolution, etc.)
    - CALVADOS3-ready output
    
    Example:
        >>> gen = PDBLMPGenerator()
        >>> xmls = gen.generate_from_pdb("6FBK")
        >>> # Returns: {"Chain_A": xml_a, "Chain_B": xml_b}
        >>> 
        >>> # Single chain
        >>> xmls = gen.generate_from_pdb("6FBK", chain_id="B")
        >>> # Returns: {"Chain_B": xml_b}
    """
    
    PDB_METADATA_API = "https://data.rcsb.org/rest/v1/core/entry"
    PDB_FASTA_API = "https://www.rcsb.org/fasta/entry"
    
    def __init__(self, cache_dir: Optional[Path] = None, rate_limit: float = 0.5):
        """
        Initialize PDB LMP generator
        
        Args:
            cache_dir: Directory for caching API responses
            rate_limit: Minimum seconds between API requests
        """
        self.cache_dir = cache_dir or Path.home() / ".lmp_cache" / "pdb"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        
        self.rate_limit = rate_limit
        self._last_request_time = 0
    
    def generate_from_pdb(
        self,
        pdb_id: str,
        chain_id: Optional[str] = None,
        state_name: Optional[str] = None
    ) -> Dict[str, str]:
        """
        Generate LMP XML documents from PDB with real PDB sequences
        
        Args:
            pdb_id: PDB ID (e.g., "6FBK")
            chain_id: Specific chain ("A", "B", etc.) or None for all chains
            state_name: Custom state name or None for auto-generated
        
        Returns:
            Dict[state_name -> XML string]
            
        Example:
            >>> gen = PDBLMPGenerator()
            >>> xmls = gen.generate_from_pdb("6FBK")
            >>> for state, xml in xmls.items():
            >>>     Path(f"6FBK_{state}.xml").write_text(xml)
        """
        # Fetch PDB data with sequences
        pdb_data = self._fetch_pdb(pdb_id)
        
        if "chains" not in pdb_data or not pdb_data["chains"]:
            raise ValueError(f"No chain sequences found for PDB {pdb_id}")
        
        chains = pdb_data["chains"]
        
        # Filter to specific chain if requested
        if chain_id:
            if chain_id not in chains:
                available = ', '.join(chains.keys())
                raise ValueError(f"Chain {chain_id} not found in PDB {pdb_id}. Available: {available}")
            chains = {chain_id: chains[chain_id]}
        
        # Generate XML for each chain
        lmp_documents = {}
        
        for cid, chain_info in chains.items():
            # Use custom state name or default
            if state_name:
                current_state = state_name
            else:
                current_state = f"Chain_{cid}"
            
            # Generate XML with PDB sequence
            xml_str = self._generate_pdb_xml(
                pdb_id=pdb_id,
                chain_id=cid,
                state_name=current_state,
                sequence=chain_info['sequence'],
                protein_name=chain_info['protein'],
                organism=chain_info.get('organism', 'Unknown'),
                pdb_data=pdb_data
            )
            
            lmp_documents[current_state] = xml_str
        
        return lmp_documents
    
    def _fetch_pdb(self, pdb_id: str) -> Dict[str, Any]:
        """
        Fetch PDB metadata + FASTA sequences
        
        Steps:
        1. GET metadata from RCSB API
        2. GET FASTA from RCSB
        3. Parse FASTA to extract chains
        4. Cache everything
        
        Returns:
            {
                "metadata": {...},
                "chains": {
                    "A": {"sequence": "...", "protein": "...", ...},
                    "B": {...}
                }
            }
        """
        cache_file = self.cache_dir / f"{pdb_id}_complete.json"
        
        # Check cache
        if cache_file.exists():
            return json.loads(cache_file.read_text())
        
        # Fetch metadata
        self._rate_limit_wait()
        metadata_url = f"{self.PDB_METADATA_API}/{pdb_id}"
        response = requests.get(metadata_url)
        response.raise_for_status()
        metadata = response.json()
        
        # Fetch FASTA
        self._rate_limit_wait()
        fasta_url = f"{self.PDB_FASTA_API}/{pdb_id}"
        fasta_response = requests.get(fasta_url)
        fasta_response.raise_for_status()
        
        # Parse chains
        chains = self._parse_pdb_fasta(fasta_response.text)
        
        # Combine
        data = {
            "metadata": metadata,
            "chains": chains,
            "fasta_raw": fasta_response.text
        }
        
        # Cache
        cache_file.write_text(json.dumps(data, indent=2))
        
        return data
    
    def _parse_pdb_fasta(self, fasta_text: str) -> Dict[str, Dict[str, str]]:
        """
        Parse PDB FASTA format
        
        Input:
            >6FBK_1|Chain A|Serine/threonine-protein kinase WNK2|Homo sapiens (9606)
            SMAEDTGVRVELAEEDHGRKS...
            >6FBK_2|Chain B[auth P]|Serine/threonine-protein kinase WNK1|Homo sapiens (9606)
            LTQVVHSAGRRFIVSPVPESRLR
        
        Output:
            {
                "A": {
                    "sequence": "SMAED...",
                    "protein": "WNK2",
                    "organism": "Homo sapiens",
                    "length": 95
                },
                "B": {...}
            }
        """
        chains = {}
        current_chain = None
        current_seq = []
        
        for line in fasta_text.strip().split('\n'):
            if line.startswith('>'):
                # Save previous chain
                if current_chain:
                    chains[current_chain['id']] = {
                        'sequence': ''.join(current_seq),
                        'protein': current_chain['protein'],
                        'organism': current_chain.get('organism', 'Unknown'),
                        'length': len(''.join(current_seq)),
                        'header': current_chain['header']
                    }
                
                # Parse header: >6FBK_1|Chain A|Protein name|Organism (taxid)
                parts = line[1:].split('|')
                
                # Extract chain ID (handle formats like "Chain A" and "Chain B[auth P]")
                chain_str = parts[1] if len(parts) > 1 else 'A'
                chain_match = re.search(r'Chain\s+([A-Z])', chain_str)
                chain_id = chain_match.group(1) if chain_match else 'A'
                
                protein_name = parts[2].strip() if len(parts) > 2 else 'Unknown'
                
                # Extract organism (remove taxid)
                organism = 'Unknown'
                if len(parts) > 3:
                    org_str = parts[3].strip()
                    # Remove (9606) style taxid
                    organism = re.sub(r'\s*\(\d+\)\s*$', '', org_str).strip()
                
                current_chain = {
                    'id': chain_id,
                    'header': line,
                    'protein': protein_name,
                    'organism': organism
                }
                current_seq = []
            else:
                current_seq.append(line.strip())
        
        # Save last chain
        if current_chain:
            chains[current_chain['id']] = {
                'sequence': ''.join(current_seq),
                'protein': current_chain['protein'],
                'organism': current_chain.get('organism', 'Unknown'),
                'length': len(''.join(current_seq)),
                'header': current_chain['header']
            }
        
        return chains
    
    def _generate_pdb_xml(
        self,
        pdb_id: str,
        chain_id: str,
        state_name: str,
        sequence: str,
        protein_name: str,
        organism: str,
        pdb_data: Dict[str, Any]
    ) -> str:
        """
        Generate LMP XML for a specific PDB chain
        
        XML Structure:
            <PML_Protein pdb_id="6FBK" chain_id="B" 
                         protein_name="WNK1" organism="Homo sapiens"
                         state="Chain_B">
              <Metadata>
                <Source type="PDB" ref="6FBK"/>
                <Method>X-RAY DIFFRACTION</Method>
                <Resolution unit="Angstrom">2.1</Resolution>
              </Metadata>
              <Chain id="B" sequence="LTQVVHSAGRRFIVSPVPESRLR">
                <Length>23</Length>
              </Chain>
            </PML_Protein>
        """
        # Root element
        root = ET.Element(
            "PML_Protein",
            pdb_id=pdb_id,
            chain_id=chain_id,
            protein_name=protein_name,
            organism=organism,
            state=state_name
        )
        
        # Metadata section
        metadata = ET.SubElement(root, "Metadata")
        ET.SubElement(metadata, "Source", type="PDB", ref=pdb_id)
        
        # Experimental method
        meta = pdb_data.get("metadata", {})
        exptl = meta.get("exptl", [])
        if exptl:
            method = exptl[0].get("method", "Unknown")
            ET.SubElement(metadata, "Method").text = method
        
        # Resolution
        rcsb_info = meta.get("rcsb_entry_info", {})
        resolution = rcsb_info.get("resolution_combined")
        if resolution:
            ET.SubElement(metadata, "Resolution", unit="Angstrom").text = f"{resolution[0]:.2f}"
        
        # Deposition date
        audit = meta.get("audit_author", [])
        if audit:
            pdbx_date = audit[0].get("pdbx_date_deposited")
            if pdbx_date:
                ET.SubElement(metadata, "DepositionDate").text = pdbx_date
        
        # Chain element
        chain_elem = ET.SubElement(root, "Chain", id=chain_id, sequence=sequence)
        ET.SubElement(chain_elem, "Length").text = str(len(sequence))
        
        # Ligands (if available)
        nonpolymer_count = rcsb_info.get("nonpolymer_entity_count", 0)
        if nonpolymer_count > 0:
            ligands_elem = ET.SubElement(chain_elem, "Ligands")
            ET.SubElement(ligands_elem, "Note").text = f"Structure contains {nonpolymer_count} non-polymer entities"
        
        # Pretty print
        return self._prettify_xml(root)
    
    def _prettify_xml(self, elem: ET.Element) -> str:
        """Pretty-print XML"""
        rough_string = ET.tostring(elem, encoding="unicode")
        reparsed = minidom.parseString(rough_string)
        return reparsed.toprettyxml(indent="  ")
    
    def _rate_limit_wait(self):
        """Enforce rate limiting"""
        elapsed = time.time() - self._last_request_time
        if elapsed < self.rate_limit:
            time.sleep(self.rate_limit - elapsed)
        self._last_request_time = time.time()
