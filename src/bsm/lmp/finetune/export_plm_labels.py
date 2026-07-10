#!/usr/bin/env python
"""
export_plm_finetune.py
======================
Export training data for **protein language models (pLMs)** like ProtT5/ESM-2.

Unlike LLMs that learn XML structure, pLMs learn from sequences.
This script converts LMP XMLs into per-residue labels suitable for:
  - PTM site prediction (binary/multiclass per residue)
  - Domain boundary detection (BIO tagging)
  - Binding site prediction
  - Conformational state classification (sequence-level)

Output formats:
  1. CSV with per-residue labels
  2. FASTA + labels (for HuggingFace datasets)
  3. JSON for flexible downstream use

Usage:
    python scripts/export_plm_finetune.py --proteins P12931,P00533
    python scripts/export_plm_finetune.py --xml-dir outputs/lmp_quickstart
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Allow running without pip install
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# NOTE:
# This script remains as a stable CLI entrypoint.
# The importable entrypoint lives in `bsm.lmp.finetune.export_plm_labels`.

from bsm.lmp.generator import LMPGenerator


def _extract_plddt_map(root: ET.Element) -> Dict[int, Tuple[float, str]]:
    """Extract per-residue pLDDT from AlphaFoldModel block.

    Returns: {residue_id: (plddt_score, confidence_class)}
    """
    plddt_map: Dict[int, Tuple[float, str]] = {}
    for ns_prefix in ("", "{http://lmp.bsm.org}"):
        for residue in root.iter(f"{ns_prefix}Residue"):
            plddt_str = residue.get("pLDDT")
            if plddt_str is not None:
                try:
                    resid = int(residue.get("id", 0))
                    plddt = float(plddt_str)
                    conf_class = residue.get("confidence_class", "")
                    if resid > 0:
                        plddt_map[resid] = (plddt, conf_class)
                except (ValueError, TypeError):
                    continue
    return plddt_map


def _extract_dssp_map(root: ET.Element) -> Dict[int, str]:
    """Extract per-residue DSSP assignment from SecondaryStructure block.

    Returns: {residue_position: ss_type} where ss_type is H/E/C
    """
    dssp_map: Dict[int, str] = {}
    for ns_prefix in ("", "{http://lmp.bsm.org}"):
        for segment in root.iter(f"{ns_prefix}Segment"):
            ss_type = segment.get("type", "C")
            try:
                start = int(segment.get("start", 0))
                end = int(segment.get("end", 0))
            except (ValueError, TypeError):
                continue
            # Map full type to single letter
            ss_letter = "C"
            if ss_type.lower().startswith("h") or "helix" in ss_type.lower():
                ss_letter = "H"
            elif ss_type.lower().startswith("e") or "strand" in ss_type.lower() or "sheet" in ss_type.lower():
                ss_letter = "E"
            elif ss_type.lower().startswith("c") or "coil" in ss_type.lower() or "loop" in ss_type.lower():
                ss_letter = "C"
            else:
                ss_letter = ss_type[0].upper() if ss_type else "C"

            for pos in range(start, end + 1):
                dssp_map[pos] = ss_letter
    return dssp_map


@dataclass
class ResidueLabel:
    """Per-residue annotation extracted from LMP."""
    position: int  # 1-indexed
    residue: str   # Single letter AA
    domain: Optional[str] = None
    domain_bio: str = "O"  # B-domain, I-domain, O (outside)
    ptm_type: Optional[str] = None  # phosphorylation, acetylation, etc.
    ptm_status: Optional[str] = None  # present, absent
    ptm_is_supervised: bool = False  # True only when label should contribute to loss
    is_binding_site: bool = False
    binding_type: Optional[str] = None  # ATP, substrate, etc.

    # Structural annotations (v4.1)
    plddt_score: Optional[float] = None          # AlphaFold pLDDT confidence (0-100)
    confidence_class: Optional[str] = None        # very_high/confident/low/very_low
    secondary_structure: Optional[str] = None     # H (helix), E (strand), C (coil)


@dataclass 
class ProteinSample:
    """Single protein sample for pLM training."""
    uniprot_id: str
    gene_name: str
    organism: str
    state: str  # Apo_Inactive, Active, etc.
    sequence: str  # Raw AA sequence (no annotations)
    residue_labels: List[ResidueLabel]
    
    # Sequence-level labels
    seq_label_state: str  # conformational state
    seq_label_ptm_count: int
    seq_label_domain_count: int

    # Structural summary labels
    seq_label_avg_plddt: Optional[float] = None
    seq_label_helix_fraction: Optional[float] = None
    seq_label_strand_fraction: Optional[float] = None
    seq_label_coil_fraction: Optional[float] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "uniprot_id": self.uniprot_id,
            "gene_name": self.gene_name,
            "organism": self.organism,
            "state": self.state,
            "sequence": self.sequence,
            "length": len(self.sequence),
            "residue_labels": [asdict(r) for r in self.residue_labels],
            "seq_label_state": self.seq_label_state,
            "seq_label_ptm_count": self.seq_label_ptm_count,
            "seq_label_domain_count": self.seq_label_domain_count,
            "seq_label_avg_plddt": self.seq_label_avg_plddt,
            "seq_label_helix_fraction": self.seq_label_helix_fraction,
            "seq_label_strand_fraction": self.seq_label_strand_fraction,
            "seq_label_coil_fraction": self.seq_label_coil_fraction,
        }


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
    
    return seq.strip()


def parse_lmp_xml(
    xml_str: str,
    *,
    absent_mode: str,
    supervise_present_only: bool,
) -> ProteinSample:
    """
    Parse LMP XML and extract per-residue labels.
    """
    root = ET.fromstring(xml_str)
    
    uniprot_id = root.get("uniprot_id", "")
    gene_name = root.get("gene_name", "")
    organism = root.get("organism", "")
    state = root.get("state", "")
    
    # Get annotated sequence from Chain
    chain = root.find(".//Chain")
    annotated_seq = chain.get("sequence", "") if chain is not None else ""
    clean_seq = extract_clean_sequence(annotated_seq)
    
    # Initialize residue labels
    residue_labels = [
        ResidueLabel(position=i+1, residue=aa)
        for i, aa in enumerate(clean_seq)
    ]
    
    # Extract PTMs
    # IMPORTANT SEMANTICS:
    # - "present" is evidence-positive.
    # - "absent" may be synthetic (state model) and is NOT a true negative by default.
    #   We control this via absent_mode and supervise_present_only.
    ptm_count = 0
    for ptm in root.findall(".//PTM"):
        pos = int(ptm.get("position", 0))
        if 1 <= pos <= len(residue_labels):
            residue_labels[pos-1].ptm_type = ptm.get("type")
            residue_labels[pos-1].ptm_status = ptm.get("status")
            status = ptm.get("status")
            if status == "present":
                residue_labels[pos-1].ptm_is_supervised = True
                ptm_count += 1
            elif status == "absent":
                if not supervise_present_only:
                    # Only supervise "absent" when the user explicitly asks.
                    residue_labels[pos-1].ptm_is_supervised = True
                else:
                    residue_labels[pos-1].ptm_is_supervised = False
            else:
                residue_labels[pos-1].ptm_is_supervised = False

            # If absent_mode=unknown, force absent to be unsupervised.
            if absent_mode == "unknown" and status == "absent":
                residue_labels[pos-1].ptm_is_supervised = False
    
    # Extract domains and assign BIO tags
    domain_count = 0
    for domain in root.findall(".//Domain"):
        domain_name = domain.get("name", "")
        start = int(domain.get("start", 0))
        end = int(domain.get("end", 0))
        domain_count += 1
        
        for pos in range(start, end + 1):
            if 1 <= pos <= len(residue_labels):
                residue_labels[pos-1].domain = domain_name
                if pos == start:
                    residue_labels[pos-1].domain_bio = f"B-{domain_name}"
                else:
                    residue_labels[pos-1].domain_bio = f"I-{domain_name}"
    
    # Extract binding sites
    for bs in root.findall(".//BindingSite"):
        residues_str = bs.get("residues", "")
        bs_type = bs.get("type", "")
        
        # Parse residue positions (format: "K123,L456" or "123-456")
        for match in re.finditer(r'([A-Z])?(\d+)', residues_str):
            pos = int(match.group(2))
            if 1 <= pos <= len(residue_labels):
                residue_labels[pos-1].is_binding_site = True
                residue_labels[pos-1].binding_type = bs_type
    
    # Enrich with structural data
    plddt_map = _extract_plddt_map(root)
    dssp_map = _extract_dssp_map(root)
    for rl in residue_labels:
        if rl.position in plddt_map:
            score, conf = plddt_map[rl.position]
            rl.plddt_score = score
            rl.confidence_class = conf
        if rl.position in dssp_map:
            rl.secondary_structure = dssp_map[rl.position]

    sample = ProteinSample(
        uniprot_id=uniprot_id,
        gene_name=gene_name,
        organism=organism,
        state=state,
        sequence=clean_seq,
        residue_labels=residue_labels,
        seq_label_state=state,
        seq_label_ptm_count=ptm_count,
        seq_label_domain_count=domain_count,
    )

    # Structural summaries
    plddt_scores = [rl.plddt_score for rl in residue_labels if rl.plddt_score is not None]
    if plddt_scores:
        sample.seq_label_avg_plddt = sum(plddt_scores) / len(plddt_scores)

    ss_labels = [rl.secondary_structure for rl in residue_labels if rl.secondary_structure]
    if ss_labels:
        total = len(ss_labels)
        sample.seq_label_helix_fraction = ss_labels.count("H") / total
        sample.seq_label_strand_fraction = ss_labels.count("E") / total
        sample.seq_label_coil_fraction = ss_labels.count("C") / total

    return sample


def export_csv(samples: List[ProteinSample], out_path: Path) -> None:
    """Export per-residue labels as CSV (one row per residue)."""
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "uniprot_id", "state", "position", "residue",
            "domain", "domain_bio", "ptm_type", "ptm_status",
            "is_binding_site", "binding_type",
            "plddt_score", "confidence_class", "secondary_structure",
        ])
        
        for sample in samples:
            for rl in sample.residue_labels:
                writer.writerow([
                    sample.uniprot_id,
                    sample.state,
                    rl.position,
                    rl.residue,
                    rl.domain or "",
                    rl.domain_bio,
                    rl.ptm_type or "",
                    rl.ptm_status or "",
                    int(rl.is_binding_site),
                    rl.binding_type or "",
                    rl.plddt_score if rl.plddt_score is not None else "",
                    rl.confidence_class or "",
                    rl.secondary_structure or "",
                ])


def export_fasta_labels(samples: List[ProteinSample], out_dir: Path) -> None:
    """
    Export in FASTA + labels format for HuggingFace datasets.
    
    Creates:
    - sequences.fasta
    - ptm_site_labels.txt (0/1; unknown positions are 0)
    - ptm_site_mask.txt (1 if supervised, 0 if unknown)
    - domain_labels.txt (BIO tags)
    - state_labels.txt (one per sequence: state string)
    """
    fasta_path = out_dir / "sequences.fasta"
    ptm_labels_path = out_dir / "ptm_site_labels.txt"
    ptm_mask_path = out_dir / "ptm_site_mask.txt"
    domain_path = out_dir / "domain_labels.txt"
    state_path = out_dir / "state_labels.txt"

    with open(fasta_path, "w") as f_fasta, \
        open(ptm_labels_path, "w") as f_ptm_labels, \
        open(ptm_mask_path, "w") as f_ptm_mask, \
        open(domain_path, "w") as f_domain, \
        open(state_path, "w") as f_state:

        for sample in samples:
            # FASTA header
            f_fasta.write(f">{sample.uniprot_id}|{sample.state}\n")
            f_fasta.write(f"{sample.sequence}\n")
            
            # PTM site labels + mask
            # - label=1 only when evidence-positive
            # - supervised negatives depend on upstream semantics; unsupervised positions have mask=0
            ptm_site_labels = [
                "1" if (rl.ptm_is_supervised and rl.ptm_status == "present") else "0"
                for rl in sample.residue_labels
            ]
            ptm_site_mask = [
                "1" if rl.ptm_is_supervised else "0" for rl in sample.residue_labels
            ]
            f_ptm_labels.write(" ".join(ptm_site_labels) + "\n")
            f_ptm_mask.write(" ".join(ptm_site_mask) + "\n")
            
            # Domain BIO labels
            domain_labels = [rl.domain_bio for rl in sample.residue_labels]
            f_domain.write(" ".join(domain_labels) + "\n")

            # Sequence-level state label (for state-conditioned heads)
            f_state.write(sample.state + "\n")

    # Structural label files
    plddt_path = out_dir / "plddt_scores.txt"
    dssp_path = out_dir / "dssp_labels.txt"
    with open(plddt_path, "w", encoding="utf-8") as f_plddt, \
         open(dssp_path, "w", encoding="utf-8") as f_dssp:
        for sample in samples:
            plddt_row = [
                f"{rl.plddt_score:.1f}" if rl.plddt_score is not None else "0.0"
                for rl in sample.residue_labels
            ]
            dssp_row = [
                rl.secondary_structure or "C"
                for rl in sample.residue_labels
            ]
            f_plddt.write(" ".join(plddt_row) + "\n")
            f_dssp.write(" ".join(dssp_row) + "\n")


def export_ptm_type_labels(
    samples: List[ProteinSample],
    out_dir: Path,
    *,
    include_absent_when_supervised: bool,
) -> None:
    """
    Export PTM type labels with a vocabulary.

    Files:
      - ptm_type_labels.txt: integer type id per residue, -1 for unknown
      - ptm_type_mask.txt: 1 if supervised, 0 otherwise
      - ptm_type_vocab.json: mapping type -> id

    Notes:
      - We only label residues where `ptm_is_supervised=True`.
      - If include_absent_when_supervised=False, supervised set typically contains only "present" sites.
    """
    types = set()
    for s in samples:
        for rl in s.residue_labels:
            if not rl.ptm_is_supervised:
                continue
            if not include_absent_when_supervised and rl.ptm_status != "present":
                continue
            if rl.ptm_type:
                types.add(rl.ptm_type)

    vocab = {t: i for i, t in enumerate(sorted(types))}

    labels_path = out_dir / "ptm_type_labels.txt"
    mask_path = out_dir / "ptm_type_mask.txt"
    vocab_path = out_dir / "ptm_type_vocab.json"

    with open(labels_path, "w", encoding="utf-8") as f_labels, open(
        mask_path, "w", encoding="utf-8"
    ) as f_mask:
        for s in samples:
            row_labels: List[str] = []
            row_mask: List[str] = []
            for rl in s.residue_labels:
                supervised = rl.ptm_is_supervised and (
                    include_absent_when_supervised or rl.ptm_status == "present"
                )
                if supervised and rl.ptm_type in vocab:
                    row_labels.append(str(vocab[rl.ptm_type]))
                    row_mask.append("1")
                else:
                    row_labels.append("-1")
                    row_mask.append("0")
            f_labels.write(" ".join(row_labels) + "\n")
            f_mask.write(" ".join(row_mask) + "\n")

    with open(vocab_path, "w", encoding="utf-8") as f_vocab:
        json.dump(vocab, f_vocab, indent=2)


def export_json(samples: List[ProteinSample], out_path: Path) -> None:
    """Export as JSON for flexible downstream use."""
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump([s.to_dict() for s in samples], f, indent=2)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Export LMP data for pLM (ProtT5/ESM-2) finetuning",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
This script converts LMP XMLs into per-residue labels for protein language models.

Unlike LLMs that learn XML structure, pLMs learn from sequences with per-residue
supervision. Use this for:
  - PTM site prediction (binary classification per residue)
  - Domain boundary detection (BIO tagging)
  - Binding site prediction
  - Multi-task learning combining all the above

Example workflow:
  1. Generate LMP XMLs: python scripts/lmp_quickstart.py
  2. Export for pLM: python scripts/export_plm_finetune.py --xml-dir outputs/lmp_quickstart
  3. Train ProtT5 with LoRA using the exported labels
        """,
    )
    ap.add_argument(
        "--proteins",
        type=str,
        help="Comma-separated UniProt IDs (generates LMP on the fly)",
    )
    ap.add_argument(
        "--xml-dir",
        type=Path,
        help="Directory with pre-generated LMP XML files",
    )
    ap.add_argument("--cache-dir", type=Path, default=Path("lmp_cache"))
    ap.add_argument("--out-dir", type=Path, default=Path("outputs/plm_finetune"))
    ap.add_argument(
        "--states",
        nargs="+",
        default=["Apo_Inactive", "Active"],
        help="Conformational states to generate",
    )
    ap.add_argument(
        "--format",
        choices=["all", "csv", "fasta", "json"],
        default="all",
        help="Output format(s)",
    )
    ap.add_argument(
        "--absent-mode",
        choices=["unknown", "negative"],
        default="unknown",
        help=(
            "How to treat PTMs with status='absent'. 'unknown' (default) masks them out; "
            "'negative' allows supervising them as 0 when requested."
        ),
    )
    ap.add_argument(
        "--ptm-supervision",
        choices=["present_only", "present_and_absent"],
        default="present_only",
        help=(
            "Which PTM statuses contribute to supervision. 'present_only' masks 'absent' sites; "
            "'present_and_absent' supervises both (only safe if absent labels are reliable)."
        ),
    )
    args = ap.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    samples: List[ProteinSample] = []
    supervise_present_only = args.ptm_supervision == "present_only"

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
                    sample = parse_lmp_xml(
                        xml_str,
                        absent_mode=args.absent_mode,
                        supervise_present_only=supervise_present_only,
                    )
                    samples.append(sample)
                    print(f"  {state}: {len(sample.sequence)} residues, "
                          f"{sample.seq_label_ptm_count} PTMs, "
                          f"{sample.seq_label_domain_count} domains")
            except Exception as e:
                print(f"  [SKIP] {e}")

    # Mode 2: Read from XML files
    elif args.xml_dir and args.xml_dir.exists():
        for xml_file in sorted(args.xml_dir.glob("*.xml")):
            print(f"Reading: {xml_file.name}")
            try:
                xml_str = xml_file.read_text(encoding="utf-8")
                sample = parse_lmp_xml(
                    xml_str,
                    absent_mode=args.absent_mode,
                    supervise_present_only=supervise_present_only,
                )
                samples.append(sample)
                print(f"  {sample.state}: {len(sample.sequence)} residues")
            except Exception as e:
                print(f"  [SKIP] {e}")

    else:
        print("ERROR: Provide --proteins or --xml-dir")
        return 1

    if not samples:
        print("No samples generated.")
        return 1

    # Export in requested format(s)
    if args.format in ("all", "csv"):
        csv_path = args.out_dir / "residue_labels.csv"
        export_csv(samples, csv_path)
        print(f"\nExported CSV: {csv_path}")

    if args.format in ("all", "fasta"):
        export_fasta_labels(samples, args.out_dir)
        export_ptm_type_labels(
            samples,
            args.out_dir,
            include_absent_when_supervised=(args.ptm_supervision == "present_and_absent"),
        )
        print(f"Exported FASTA+labels: {args.out_dir}/sequences.fasta")

    if args.format in ("all", "json"):
        json_path = args.out_dir / "samples.json"
        export_json(samples, json_path)
        print(f"Exported JSON: {json_path}")

    # Summary
    total_residues = sum(len(s.sequence) for s in samples)
    total_ptms = sum(s.seq_label_ptm_count for s in samples)
    print(f"\n=== Summary ===")
    print(f"Samples: {len(samples)}")
    print(f"Total residues: {total_residues}")
    print(f"Total PTMs (present): {total_ptms}")
    print(f"Output dir: {args.out_dir}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
