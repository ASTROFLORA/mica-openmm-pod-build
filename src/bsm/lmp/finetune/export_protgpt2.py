#!/usr/bin/env python
"""
export_protgpt2.py
==================
Export training data for **ProtGPT2** (Causal Language Model).

This script converts LMP XMLs into a tagged sequence format suitable for
conditional generation. It extracts rich metadata (domains, PTMs, state, etc.)
and prepends them as control tags before the sequence.

Format:
<|endoftext|><uniprot>ID</uniprot><state>Active</state>... SEQUENCE

Usage:
    python scripts/export_protgpt2.py --proteins P12931 --out-dir outputs/protgpt2
"""
from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import List, Set

# Allow running without pip install
_SRC = Path(__file__).resolve().parent.parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from bsm.lmp.generator import LMPGenerator


@dataclass
class ProtGPT2Sample:
    """Single protein sample for ProtGPT2 training."""
    uniprot_id: str
    gene_name: str
    organism: str
    state: str
    sequence: str
    domains: List[str]
    ptms: List[str]
    ligands: List[str]

    def to_tagged_text(self) -> str:
        """Convert to tagged text format for conditional generation."""
        # Sort lists for deterministic ordering
        domains_str = ",".join(sorted(set(self.domains)))
        ptms_str = ",".join(sorted(set(self.ptms)))
        ligands_str = ",".join(sorted(set(self.ligands)))

        # Build the prompt with tags
        # Note: We use standard XML-like tags. 
        # Ensure these are added to the tokenizer as special tokens if possible,
        # or rely on the model learning them as text patterns.
        parts = [
            "<|endoftext|>",
            f"<uniprot>{self.uniprot_id}</uniprot>",
            f"<gene>{self.gene_name}</gene>",
            f"<organism>{self.organism}</organism>",
            f"<state>{self.state}</state>",
        ]
        
        if domains_str:
            parts.append(f"<domains>{domains_str}</domains>")
        if ptms_str:
            parts.append(f"<ptms>{ptms_str}</ptms>")
        if ligands_str:
            parts.append(f"<ligands>{ligands_str}</ligands>")
            
        # Add sequence (separated by space or newline? Usually just appended)
        # We add a newline for readability in the dataset file, 
        # but the tokenizer will handle it.
        parts.append("\n" + self.sequence)
        
        return "".join(parts)


def extract_clean_sequence(annotated_seq: str) -> str:
    """
    Extract clean AA sequence from LMP annotated sequence.
    Removes: [DOM:...], [/DOM], {Myr}, {P}, [BIND:...], [/BIND], etc.
    """
    # Remove domain markers
    seq = re.sub(r'\[DOM:[^\]]+\]', '', annotated_seq)
    seq = re.sub(r'\[/DOM\]', '', seq)
    
    # Remove binding site markers
    seq = re.sub(r'\[BIND:[^\]]+\]', '', seq)
    seq = re.sub(r'\[/BIND\]', '', seq)
    
    # Remove PTM markers like {Myr}, {P}, etc.
    seq = re.sub(r'\{[^}]+\}', '', seq)
    
    # Remove any remaining brackets
    seq = re.sub(r'[\[\]]', '', seq)
    
    # Remove whitespace (newlines in XML sequence)
    return "".join(seq.split())


def parse_lmp_to_protgpt2(xml_str: str) -> ProtGPT2Sample:
    """Parse LMP XML into a ProtGPT2 sample."""
    root = ET.fromstring(xml_str)
    
    uniprot_id = root.get("uniprot_id", "")
    gene_name = root.get("gene_name", "")
    organism = root.get("organism", "")
    state = root.get("state", "")
    
    # Sequence
    chain = root.find(".//Chain")
    annotated_seq = chain.get("sequence", "") if chain is not None else ""
    clean_seq = extract_clean_sequence(annotated_seq)
    
    # Domains
    domains = []
    for dom in root.findall(".//Domain"):
        name = dom.get("name")
        if name:
            domains.append(name)
            
    # PTMs
    ptms = []
    for ptm in root.findall(".//PTM"):
        ptm_type = ptm.get("type")
        status = ptm.get("status")
        # We only list PTMs that are present or explicitly noted
        if ptm_type and status == "present":
            ptms.append(ptm_type)
            
    # Ligands (from BindingSites)
    ligands = []
    for bs in root.findall(".//BindingSite"):
        ligand = bs.get("ligand")
        if ligand:
            ligands.append(ligand)
        # Also check 'type' if ligand is not specified but type implies it (e.g. ATP)
        bs_type = bs.get("type")
        if bs_type and not ligand:
            ligands.append(bs_type)

    return ProtGPT2Sample(
        uniprot_id=uniprot_id,
        gene_name=gene_name,
        organism=organism,
        state=state,
        sequence=clean_seq,
        domains=domains,
        ptms=ptms,
        ligands=ligands,
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Export LMP data for ProtGPT2 fine-tuning",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--proteins",
        type=str,
        help="Comma-separated UniProt IDs",
    )
    ap.add_argument(
        "--xml-dir",
        type=Path,
        help="Directory with pre-generated LMP XML files",
    )
    ap.add_argument("--cache-dir", type=Path, default=Path("lmp_cache"))
    ap.add_argument("--out-dir", type=Path, default=Path("outputs/protgpt2"))
    ap.add_argument(
        "--states",
        nargs="+",
        default=["Apo_Inactive", "Active"],
        help="Conformational states to generate",
    )
    
    args = ap.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    
    samples: List[ProtGPT2Sample] = []
    
    # Mode 1: Generate from UniProt IDs
    if args.proteins:
        generator = LMPGenerator(cache_dir=args.cache_dir)
        protein_ids = [p.strip() for p in args.proteins.split(",") if p.strip()]
        
        for uid in protein_ids:
            print(f"Processing: {uid}")
            try:
                xml_by_state = generator.generate_multi_state(
                    uniprot_id=uid,
                    gene_name=uid,
                    states=args.states,
                )
                for state, xml_str in xml_by_state.items():
                    sample = parse_lmp_to_protgpt2(xml_str)
                    samples.append(sample)
            except Exception as e:
                print(f"  [SKIP] {e}")

    # Mode 2: Read from XML files
    elif args.xml_dir and args.xml_dir.exists():
        for xml_file in sorted(args.xml_dir.glob("*.xml")):
            print(f"Reading: {xml_file.name}")
            try:
                xml_str = xml_file.read_text(encoding="utf-8")
                sample = parse_lmp_to_protgpt2(xml_str)
                samples.append(sample)
            except Exception as e:
                print(f"  [SKIP] {e}")
    else:
        print("ERROR: Provide --proteins or --xml-dir")
        return 1

    if not samples:
        print("No samples generated.")
        return 1

    # Export to text file
    out_file = args.out_dir / "protgpt2_dataset.txt"
    with open(out_file, "w", encoding="utf-8") as f:
        for s in samples:
            f.write(s.to_tagged_text() + "\n")
            
    print(f"\n=== Summary ===")
    print(f"Samples: {len(samples)}")
    print(f"Exported to: {out_file}")
    
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
