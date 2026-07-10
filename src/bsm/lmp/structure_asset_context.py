"""LMP structure asset context detection for imported PDB files.

This module turns a local, user-provided PDB into a metadata-first LMP context
record. It is deterministic and offline by default. When explicitly enabled,
the detector can run infrastructure-backed sequence resolution through RCSB
sequence search, SIFTS-backed RCSB entity metadata, and UniProt feature records.
"""
from __future__ import annotations

import hashlib
import asyncio
import json
import math
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


AMINO_ACID_3_TO_1: Dict[str, str] = {
    "ALA": "A",
    "ARG": "R",
    "ASN": "N",
    "ASP": "D",
    "CYS": "C",
    "GLN": "Q",
    "GLU": "E",
    "GLY": "G",
    "HIS": "H",
    "ILE": "I",
    "LEU": "L",
    "LYS": "K",
    "MET": "M",
    "PHE": "F",
    "PRO": "P",
    "SER": "S",
    "THR": "T",
    "TRP": "W",
    "TYR": "Y",
    "VAL": "V",
    "SEC": "U",
    "PYL": "O",
    "MSE": "M",
    "ASX": "B",
    "GLX": "Z",
    "XLE": "J",
    "UNK": "X",
}

_AMINO_ACID_LETTERS = frozenset("ABCDEFGHIKLMNPQRSTUVWYZOJX")
_NESY_TAG_PATTERN = re.compile(r"\[(?P<close>/)?(?P<kind>[A-Z]+)(?::(?P<name>[^\]]+))?\]")
_RCSB_SEQUENCE_SEARCH_URL = "https://search.rcsb.org/rcsbsearch/v2/query"
_RCSB_POLYMER_ENTITY_URL = "https://data.rcsb.org/rest/v1/core/polymer_entity/{entry_id}/{entity_id}"
_UNIPROT_JSON_URL = "https://rest.uniprot.org/uniprotkb/{accession}.json"


@dataclass(frozen=True)
class PDBAtomObservation:
    """Observed atom with residue identity and Cartesian coordinates."""

    chain_id: str
    atom_name: str
    residue_name: str
    sequence_number: int
    insertion_code: str
    element: str
    x: float
    y: float
    z: float
    record_type: str = "ATOM"

    def residue_key(self) -> Tuple[int, str]:
        return self.sequence_number, self.insertion_code

    def residue_label(self) -> str:
        insertion_suffix = self.insertion_code if self.insertion_code else ""
        return f"{self.chain_id}:{self.residue_name}{self.sequence_number}{insertion_suffix}"

    def is_hydrogen(self) -> bool:
        atom_name = self.atom_name.strip().upper()
        element = self.element.strip().upper()
        return element in {"H", "D"} or atom_name.startswith("H") or atom_name.startswith("D")


@dataclass(frozen=True)
class PDBResidueObservation:
    """Observed residue key from coordinate records."""

    residue_name: str
    sequence_number: int
    insertion_code: str = ""
    record_type: str = "ATOM"

    def residue_id(self) -> str:
        insertion_suffix = self.insertion_code if self.insertion_code else ""
        return f"{self.residue_name}{self.sequence_number}{insertion_suffix}"


@dataclass
class PDBChainContext:
    """Per-chain structural context reconstructed from a PDB file."""

    chain_id: str
    sequence: str
    residue_count: int
    atom_count: int
    ca_atom_count: int
    first_residue_number: Optional[int]
    last_residue_number: Optional[int]
    residue_ranges: List[Dict[str, Any]] = field(default_factory=list)
    numbering_gaps: List[Dict[str, int]] = field(default_factory=list)
    insertion_codes: List[str] = field(default_factory=list)
    unknown_residues: List[Dict[str, Any]] = field(default_factory=list)
    seqres_sequence: str = ""
    db_refs: List[Dict[str, Any]] = field(default_factory=list)
    sequence_source: str = "atom_records"

    @property
    def sequence_sha256(self) -> str:
        return hashlib.sha256(self.sequence.encode("utf-8")).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["sequence_sha256"] = self.sequence_sha256
        payload["sequence_length"] = len(self.sequence)
        return payload


@dataclass
class StructureAssetRecord:
    """Metadata record for a user-provided structural asset."""

    asset_id: str
    source_kind: str
    structure_format: str
    local_path: str
    sha256: str
    size_bytes: int
    chain_ids: List[str]
    source_profile: str
    privacy_decision: str
    provenance: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PDBStructureAssetContext:
    """Full LMP bridge context for an imported PDB asset."""

    asset: StructureAssetRecord
    header: Dict[str, Any]
    chains: List[PDBChainContext]
    multimer: Dict[str, Any]
    identity_resolution: Dict[str, Any]
    biological_context: Dict[str, Any]
    physical_context: Dict[str, Any]
    lmp_attachment: Dict[str, Any]
    smic_handoff: Dict[str, Any]
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "asset": self.asset.to_dict(),
            "header": self.header,
            "chains": [chain.to_dict() for chain in self.chains],
            "multimer": self.multimer,
            "identity_resolution": self.identity_resolution,
            "biological_context": self.biological_context,
            "physical_context": self.physical_context,
            "lmp_attachment": self.lmp_attachment,
            "smic_handoff": self.smic_handoff,
            "warnings": self.warnings,
        }


@dataclass
class _MutableChain:
    chain_id: str
    residues: Dict[Tuple[int, str], PDBResidueObservation] = field(default_factory=dict)
    residue_order: List[Tuple[int, str]] = field(default_factory=list)
    atoms: List[PDBAtomObservation] = field(default_factory=list)
    atom_count: int = 0
    ca_atom_count: int = 0

    def observe_atom(self, atom: PDBAtomObservation) -> None:
        residue = PDBResidueObservation(
            residue_name=atom.residue_name,
            sequence_number=atom.sequence_number,
            insertion_code=atom.insertion_code,
            record_type=atom.record_type,
        )
        residue_key = (residue.sequence_number, residue.insertion_code)
        if residue_key not in self.residues:
            self.residues[residue_key] = residue
            self.residue_order.append(residue_key)
        self.atoms.append(atom)
        self.atom_count += 1
        if atom.atom_name.strip().upper() == "CA":
            self.ca_atom_count += 1


def detect_pdb_structure_context(
    pdb_path: str | Path,
    *,
    asset_id: Optional[str] = None,
    privacy_decision: str = "local_metadata_only",
    enable_remote_sequence_search: bool = False,
    enable_remote_mmseqs2: bool = False,
    enable_remote_blast: Optional[bool] = None,
    remote_identity_timeout_seconds: int = 30,
    local_lmp_roots: Optional[Sequence[str | Path]] = None,
) -> PDBStructureAssetContext:
    """Detect LMP-ready biological structure context from a local PDB file.

    The detector reconstructs chain sequences from coordinate records, carries
    any header identity hints, and resolves original biological identity only
    from explicit metadata, local LMP exact sequence matches, or an explicitly
    enabled RCSB/SIFTS/UniProt lookup.
    """
    if enable_remote_blast is not None:
        enable_remote_mmseqs2 = enable_remote_mmseqs2 or enable_remote_blast

    path = Path(pdb_path)
    if not path.exists():
        raise FileNotFoundError(f"PDB file not found: {path}")

    file_bytes = path.read_bytes()
    file_sha256 = hashlib.sha256(file_bytes).hexdigest()
    asset_identifier = asset_id or f"structure_asset:{file_sha256[:16]}"

    header, mutable_chains, warnings = _parse_pdb_lines(
        file_bytes.decode("utf-8", errors="replace").splitlines()
    )
    chains = _finalize_chains(mutable_chains, header)
    chain_ids = [chain.chain_id for chain in chains]
    source_profile = "openmm_prepared_pdb" if header.get("created_by_openmm") else "pdb"

    asset = StructureAssetRecord(
        asset_id=asset_identifier,
        source_kind="imported_user_asset",
        structure_format="pdb",
        local_path=str(path),
        sha256=file_sha256,
        size_bytes=len(file_bytes),
        chain_ids=chain_ids,
        source_profile=source_profile,
        privacy_decision=privacy_decision,
        provenance={
            "detector": "bsm.lmp.structure_asset_context.detect_pdb_structure_context",
            "metadata_only": not enable_remote_sequence_search,
            "remote_identity_lookup_executed": enable_remote_sequence_search,
            "remote_blast_requested": enable_remote_mmseqs2,
        },
    )

    lmp_roots = _default_lmp_roots() if local_lmp_roots is None else [Path(root) for root in local_lmp_roots]
    multimer = _build_multimer_summary(chains)
    physical_context = _build_physical_context(mutable_chains, chains)
    identity_resolution = _build_identity_resolution(
        header,
        chains,
        local_lmp_roots=lmp_roots,
        enable_remote_sequence_search=enable_remote_sequence_search,
        enable_remote_mmseqs2=enable_remote_mmseqs2,
        remote_identity_timeout_seconds=remote_identity_timeout_seconds,
    )
    biological_context = _build_biological_context(identity_resolution, chains)
    lmp_attachment = _build_lmp_attachment(asset, chains, identity_resolution)
    smic_handoff = _build_smic_handoff(asset, chains, multimer, physical_context)

    if not chains:
        warnings.append("No polymer chain residues were reconstructed from ATOM/HETATM records.")
    if identity_resolution["status"] == "unresolved_identity":
        warnings.append(
            "No DBREF identity was present; UniProt/BLAST resolution is required before claiming original protein identity."
        )
    elif identity_resolution["status"] == "partially_resolved_identity":
        warnings.append(
            "Only a subset of polymer chains resolved to biological identity; unresolved chains require BLAST/UniProt acceptance before final labeling."
        )

    return PDBStructureAssetContext(
        asset=asset,
        header=header,
        chains=chains,
        multimer=multimer,
        identity_resolution=identity_resolution,
        biological_context=biological_context,
        physical_context=physical_context,
        lmp_attachment=lmp_attachment,
        smic_handoff=smic_handoff,
        warnings=warnings,
    )


def _parse_pdb_lines(lines: Iterable[str]) -> Tuple[Dict[str, Any], Dict[str, _MutableChain], List[str]]:
    header: Dict[str, Any] = {
        "title": [],
        "compound": [],
        "source": [],
        "db_refs": [],
        "seqres": {},
        "remarks": [],
        "created_by_openmm": False,
        "openmm_remark": "",
        "model_count": 0,
        "total_atom_records": 0,
        "parsed_atom_records": 0,
        "heterogen_residue_count": 0,
    }
    mutable_chains: Dict[str, _MutableChain] = {}
    warnings: List[str] = []
    current_model: Optional[int] = None
    first_model_closed = False

    for raw_line in lines:
        line = raw_line.rstrip("\n")
        record_type = line[0:6].strip().upper()

        if record_type == "HEADER":
            header["classification"] = line[10:50].strip()
            header["deposition_date"] = line[50:59].strip()
            header["pdb_id"] = line[62:66].strip()
        elif record_type == "TITLE":
            header["title"].append(line[10:].strip())
        elif record_type == "COMPND":
            header["compound"].append(line[10:].strip())
        elif record_type == "SOURCE":
            header["source"].append(line[10:].strip())
        elif record_type == "REMARK":
            _capture_remark(header, line)
        elif record_type == "CRYST1":
            header["unit_cell"] = _parse_cryst1(line)
        elif record_type == "DBREF":
            header["db_refs"].append(_parse_dbref(line))
        elif record_type == "SEQRES":
            _capture_seqres(header, line)
        elif record_type == "MODEL":
            header["model_count"] += 1
            current_model = header["model_count"]
        elif record_type == "ENDMDL":
            if current_model == 1:
                first_model_closed = True
            current_model = None
        elif record_type in {"ATOM", "HETATM"}:
            header["total_atom_records"] += 1
            if current_model not in (None, 1) or first_model_closed:
                continue
            parsed = _parse_coordinate_record(line, record_type)
            if parsed is None:
                continue
            atom, is_polymer_record = parsed
            if not is_polymer_record:
                header["heterogen_residue_count"] += 1
                continue
            chain_id = atom.chain_id
            mutable_chain = mutable_chains.setdefault(chain_id, _MutableChain(chain_id=chain_id))
            mutable_chain.observe_atom(atom)
            header["parsed_atom_records"] += 1

    return header, mutable_chains, warnings


def _capture_remark(header: Dict[str, Any], line: str) -> None:
    text = line[10:].strip()
    if not text:
        return
    if len(header["remarks"]) < 20:
        header["remarks"].append(text)
    if "OPENMM" in text.upper():
        header["created_by_openmm"] = True
        if not header["openmm_remark"]:
            header["openmm_remark"] = text


def _parse_cryst1(line: str) -> Dict[str, Any]:
    return {
        "a": _parse_float(line[6:15]),
        "b": _parse_float(line[15:24]),
        "c": _parse_float(line[24:33]),
        "alpha": _parse_float(line[33:40]),
        "beta": _parse_float(line[40:47]),
        "gamma": _parse_float(line[47:54]),
        "space_group": line[55:66].strip(),
    }


def _parse_dbref(line: str) -> Dict[str, Any]:
    fixed_width = {
        "pdb_id": line[7:11].strip(),
        "chain_id": line[12:13].strip() or "_",
        "pdb_start": _parse_int(line[14:18]),
        "pdb_start_insertion": line[18:19].strip(),
        "pdb_end": _parse_int(line[20:24]),
        "pdb_end_insertion": line[24:25].strip(),
        "database": line[26:32].strip(),
        "accession": line[33:41].strip(),
        "database_id": line[42:54].strip(),
        "database_start": _parse_int(line[55:60]),
        "database_end": _parse_int(line[62:67]),
    }
    if fixed_width["database"] or fixed_width["accession"]:
        return fixed_width

    tokens = line.split()
    return {
        "pdb_id": tokens[1] if len(tokens) > 1 else "",
        "chain_id": tokens[2] if len(tokens) > 2 else "_",
        "pdb_start": _parse_int(tokens[3]) if len(tokens) > 3 else None,
        "pdb_start_insertion": "",
        "pdb_end": _parse_int(tokens[4]) if len(tokens) > 4 else None,
        "pdb_end_insertion": "",
        "database": tokens[5] if len(tokens) > 5 else "",
        "accession": tokens[6] if len(tokens) > 6 else "",
        "database_id": tokens[7] if len(tokens) > 7 else "",
        "database_start": _parse_int(tokens[8]) if len(tokens) > 8 else None,
        "database_end": _parse_int(tokens[9]) if len(tokens) > 9 else None,
    }


def _capture_seqres(header: Dict[str, Any], line: str) -> None:
    chain_id = line[11:12].strip() or "_"
    residue_names = line[19:].split()
    sequence = "".join(AMINO_ACID_3_TO_1.get(residue_name.upper(), "X") for residue_name in residue_names)
    header["seqres"].setdefault(chain_id, "")
    header["seqres"][chain_id] += sequence


def _parse_coordinate_record(
    line: str,
    record_type: str,
) -> Optional[Tuple[PDBAtomObservation, bool]]:
    if len(line) < 27:
        return None
    atom_name = line[12:16].strip()
    residue_name = line[17:20].strip().upper()
    chain_id = line[21:22].strip() or "_"
    sequence_number = _parse_int(line[22:26])
    insertion_code = line[26:27].strip()
    x = _parse_float(line[30:38])
    y = _parse_float(line[38:46])
    z = _parse_float(line[46:54])
    if sequence_number is None or not residue_name:
        return None
    if x is None or y is None or z is None:
        return None

    is_known_polymer = residue_name in AMINO_ACID_3_TO_1
    is_polymer_record = record_type == "ATOM" or (record_type == "HETATM" and is_known_polymer)
    element = line[76:78].strip().upper() if len(line) >= 78 else ""
    if not element:
        element = atom_name[:1].strip().upper()
    atom = PDBAtomObservation(
        chain_id=chain_id,
        atom_name=atom_name,
        residue_name=residue_name,
        sequence_number=sequence_number,
        insertion_code=insertion_code,
        element=element,
        x=x,
        y=y,
        z=z,
        record_type=record_type,
    )
    return atom, is_polymer_record


def _finalize_chains(
    mutable_chains: Dict[str, _MutableChain],
    header: Dict[str, Any],
) -> List[PDBChainContext]:
    db_refs_by_chain: Dict[str, List[Dict[str, Any]]] = {}
    for db_ref in header.get("db_refs", []):
        db_refs_by_chain.setdefault(db_ref.get("chain_id") or "_", []).append(db_ref)

    chains: List[PDBChainContext] = []
    for chain_id in sorted(mutable_chains):
        mutable_chain = mutable_chains[chain_id]
        residues = [mutable_chain.residues[key] for key in mutable_chain.residue_order]
        sequence = "".join(AMINO_ACID_3_TO_1.get(residue.residue_name, "X") for residue in residues)
        residue_numbers = [residue.sequence_number for residue in residues]
        insertion_codes = sorted({residue.insertion_code for residue in residues if residue.insertion_code})
        seqres_sequence = header.get("seqres", {}).get(chain_id, "")
        sequence_source = "atom_records"
        if seqres_sequence and seqres_sequence == sequence:
            sequence_source = "seqres_and_atom_records"
        elif seqres_sequence:
            sequence_source = "atom_records_with_seqres_context"

        chains.append(PDBChainContext(
            chain_id=chain_id,
            sequence=sequence,
            residue_count=len(residues),
            atom_count=mutable_chain.atom_count,
            ca_atom_count=mutable_chain.ca_atom_count,
            first_residue_number=min(residue_numbers) if residue_numbers else None,
            last_residue_number=max(residue_numbers) if residue_numbers else None,
            residue_ranges=_build_residue_ranges(residues),
            numbering_gaps=_build_numbering_gaps(residues),
            insertion_codes=insertion_codes,
            unknown_residues=_build_unknown_residues(residues),
            seqres_sequence=seqres_sequence,
            db_refs=db_refs_by_chain.get(chain_id, []),
            sequence_source=sequence_source,
        ))
    return chains


def _build_residue_ranges(residues: List[PDBResidueObservation]) -> List[Dict[str, Any]]:
    if not residues:
        return []
    ranges: List[Dict[str, Any]] = []
    start_residue = residues[0]
    previous_residue = residues[0]
    for residue in residues[1:]:
        is_contiguous = residue.sequence_number <= previous_residue.sequence_number + 1
        if not is_contiguous:
            ranges.append(_range_payload(start_residue, previous_residue))
            start_residue = residue
        previous_residue = residue
    ranges.append(_range_payload(start_residue, previous_residue))
    return ranges


def _range_payload(start_residue: PDBResidueObservation, end_residue: PDBResidueObservation) -> Dict[str, Any]:
    return {
        "start": start_residue.sequence_number,
        "start_insertion": start_residue.insertion_code,
        "end": end_residue.sequence_number,
        "end_insertion": end_residue.insertion_code,
    }


def _build_numbering_gaps(residues: List[PDBResidueObservation]) -> List[Dict[str, int]]:
    gaps: List[Dict[str, int]] = []
    for previous_residue, residue in zip(residues, residues[1:]):
        missing_count = residue.sequence_number - previous_residue.sequence_number - 1
        if missing_count > 0:
            gaps.append({
                "after": previous_residue.sequence_number,
                "before": residue.sequence_number,
                "missing_count": missing_count,
            })
    return gaps


def _build_unknown_residues(residues: List[PDBResidueObservation]) -> List[Dict[str, Any]]:
    unknown: List[Dict[str, Any]] = []
    for residue in residues:
        if AMINO_ACID_3_TO_1.get(residue.residue_name) == "X":
            unknown.append({
                "residue_name": residue.residue_name,
                "sequence_number": residue.sequence_number,
                "insertion_code": residue.insertion_code,
            })
    return unknown


def _build_multimer_summary(chains: List[PDBChainContext]) -> Dict[str, Any]:
    groups_by_sequence: Dict[str, Dict[str, Any]] = {}
    for chain in chains:
        groups_by_sequence.setdefault(chain.sequence_sha256, {
            "sequence_sha256": chain.sequence_sha256,
            "sequence_length": len(chain.sequence),
            "chain_ids": [],
        })["chain_ids"].append(chain.chain_id)

    repeated_groups = [
        group for group in groups_by_sequence.values() if len(group["chain_ids"]) > 1
    ]
    chain_count = len(chains)
    unique_sequence_count = len(groups_by_sequence)
    if chain_count == 0:
        assembly_classification = "no_polymer_chains"
    elif chain_count == 1:
        assembly_classification = "single_chain"
    elif unique_sequence_count == 1:
        assembly_classification = "homomeric_candidate"
    else:
        assembly_classification = "heteromeric_or_fragmented_candidate"

    return {
        "chain_count": chain_count,
        "unique_sequence_count": unique_sequence_count,
        "assembly_classification": assembly_classification,
        "repeated_sequence_groups": repeated_groups,
    }


def _build_identity_resolution(
    header: Dict[str, Any],
    chains: List[PDBChainContext],
    *,
    local_lmp_roots: Sequence[Path],
    enable_remote_sequence_search: bool,
    remote_identity_timeout_seconds: int,
    enable_remote_mmseqs2: bool,
) -> Dict[str, Any]:
    db_refs = header.get("db_refs", [])
    seqres = header.get("seqres", {})
    available_hints: List[Dict[str, Any]] = []
    for db_ref in db_refs:
        available_hints.append({
            "chain_id": db_ref.get("chain_id"),
            "database": db_ref.get("database"),
            "accession": db_ref.get("accession"),
            "database_id": db_ref.get("database_id"),
            "pdb_range": [db_ref.get("pdb_start"), db_ref.get("pdb_end")],
            "database_range": [db_ref.get("database_start"), db_ref.get("database_end")],
        })

    chain_resolutions: List[Dict[str, Any]] = []
    remote_errors: List[Dict[str, Any]] = []
    for chain in chains:
        metadata_hints = [hint for hint in available_hints if hint.get("chain_id") == chain.chain_id]
        local_matches = _resolve_local_lmp_matches(chain.sequence, local_lmp_roots)
        remote_resolution = _empty_remote_resolution(executed=enable_remote_sequence_search)
        if enable_remote_sequence_search:
            remote_resolution = _resolve_remote_chain_identity(
                chain,
                timeout_seconds=remote_identity_timeout_seconds,
                enable_remote_mmseqs2=enable_remote_mmseqs2,
            )
            for error in remote_resolution.get("errors", []):
                remote_errors.append({"chain_id": chain.chain_id, "error": error})

        accepted_identity = _select_accepted_identity(
            chain,
            metadata_hints=metadata_hints,
            local_matches=local_matches,
            remote_resolution=remote_resolution,
        )
        if accepted_identity and accepted_identity.get("confidence") == "metadata_only":
            chain_status = "metadata_identity_available"
        else:
            chain_status = "resolved" if accepted_identity else "unresolved"
        chain_resolutions.append({
            "chain_id": chain.chain_id,
            "status": chain_status,
            "sequence_length": len(chain.sequence),
            "sequence_sha256": chain.sequence_sha256,
            "accepted_identity": accepted_identity,
            "metadata_hints": metadata_hints,
            "local_lmp_matches": local_matches,
            "remote_sequence_search": remote_resolution,
            "domain_context": _collect_chain_domain_context(accepted_identity, local_matches, remote_resolution),
        })

    resolved_count = sum(
        1
        for resolution in chain_resolutions
        if resolution["accepted_identity"]
        and resolution["accepted_identity"].get("confidence") != "metadata_only"
    )
    if chains and resolved_count == len(chains):
        status = "resolved_identity"
    elif resolved_count:
        status = "partially_resolved_identity"
    elif db_refs:
        status = "metadata_identity_available"
    else:
        status = "unresolved_identity"

    return {
        "status": status,
        "resolved_chain_count": resolved_count,
        "unresolved_chain_count": max(0, len(chains) - resolved_count),
        "evidence": {
            "dbref_present": bool(db_refs),
            "seqres_present": bool(seqres),
            "compound_present": bool(header.get("compound")),
            "source_present": bool(header.get("source")),
            "atom_sequence_reconstructed": bool(chains),
            "local_lmp_exact_lookup_executed": True,
            "local_lmp_roots": [str(root) for root in local_lmp_roots if root.exists()],
            "remote_identity_lookup_executed": enable_remote_sequence_search,
            "remote_blast_executed": enable_remote_sequence_search and enable_remote_mmseqs2,
            "remote_identity_errors": remote_errors,
        },
        "available_hints": available_hints,
        "chain_resolutions": chain_resolutions,
        "query_sequences": [
            {
                "chain_id": chain.chain_id,
                "sequence": chain.sequence,
                "sequence_length": len(chain.sequence),
                "sequence_sha256": chain.sequence_sha256,
            }
            for chain in chains
        ],
        "recommended_resolution_steps": [
            "Run remote blastp via bsm.alignment.blast_integration.BlastService and RCSB sequence search for each reconstructed chain sequence when local metadata is insufficient.",
            "Map accepted hits to ProteinIdentity with bsm.identity_resolver before claiming UniProt identity.",
            "If a PDB hit is accepted, reconcile hit chain ranges against residue_ranges before labeling original PDB fragments.",
        ],
    }


def _build_lmp_attachment(
    asset: StructureAssetRecord,
    chains: List[PDBChainContext],
    identity_resolution: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "context_schema": "mica.lmp.structure_asset_context.v1",
        "attach_to": [
            "StructureCatalog",
            "StructureSet",
            "ResidueStatistics",
            "TopologyHooks",
        ],
        "protocol_nodes": [
            {
                "node_kind": "REGISTER_STRUCTURE_ASSET",
                "executor_surface": "lmp_structure_asset_registry",
                "inputs": {"asset_id": asset.asset_id, "structure_format": asset.structure_format},
            },
            {
                "node_kind": "LMP_ATTACH_STRUCTURE_CONTEXT",
                "executor_surface": "lmp_cartographer",
                "inputs": {
                    "asset_id": asset.asset_id,
                    "chain_ids": [chain.chain_id for chain in chains],
                    "identity_status": identity_resolution.get("status", "unresolved_identity"),
                },
            },
        ],
    }


def _build_smic_handoff(
    asset: StructureAssetRecord,
    chains: List[PDBChainContext],
    multimer: Dict[str, Any],
    physical_context: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "ready": bool(chains),
        "payload_kind": "lmp_structure_context_v2",
        "asset_id": asset.asset_id,
        "chain_ids": [chain.chain_id for chain in chains],
        "assembly_classification": multimer["assembly_classification"],
        "static_contact_analysis": {
            "available": physical_context.get("status") == "contacts_computed",
            "analysis_kind": physical_context.get("analysis_kind"),
            "chain_pair_count": len(physical_context.get("chain_pairs", [])),
            "has_inter_chain_contacts": physical_context.get("has_inter_chain_contacts", False),
            "contact_cutoffs_angstrom": physical_context.get("cutoffs_angstrom", {}),
        },
        "execution_boundary": "LMP emits structural context only; SMIC/MD execution remains downstream and approval-gated.",
        "next_smic_steps": [
            "Use this static contact payload as the zero-frame SMIC handoff.",
            "Run SMIC trajectory contact/network analysis only after topology/trajectory approval.",
            "Promote stable inter-chain contacts to ResidueStatistics and interaction-network ledgers after SMIC execution.",
        ],
    }


def _default_lmp_roots() -> List[Path]:
    lmp_root = Path(__file__).resolve().parent
    repo_root = lmp_root.parents[2]
    cwd_root = Path.cwd()
    roots = [
        lmp_root / "output_all_presets",
        lmp_root / "test_output_v4_enriched",
        repo_root / ".mica" / "logs",
        cwd_root / ".mica" / "logs",
    ]
    unique_roots: List[Path] = []
    seen = set()
    for root in roots:
        key = str(root.resolve()) if root.exists() else str(root)
        if key in seen:
            continue
        seen.add(key)
        unique_roots.append(root)
    return unique_roots


def _empty_remote_resolution(*, executed: bool) -> Dict[str, Any]:
    return {
        "executed": executed,
        "services": [],
        "accepted_identity": None,
        "candidate_hits": [],
        "ncbi_blast": {"executed": False, "hits": [], "errors": []},
        "errors": [],
    }


def _resolve_local_lmp_matches(
    sequence: str,
    roots: Sequence[Path],
    max_matches: int = 8,
    max_files: int = 512,
) -> List[Dict[str, Any]]:
    if len(sequence) < 6:
        return []
    matches: List[Dict[str, Any]] = []
    seen = set()
    scanned_files = 0
    for xml_path in _iter_lmp_xml_files(roots):
        scanned_files += 1
        if scanned_files > max_files:
            break
        parsed = _parse_lmp_xml_for_sequences(xml_path)
        if parsed is None:
            continue
        for source in parsed["sequences"]:
            source_sequence = source["plain_sequence"]
            match_start_zero = source_sequence.find(sequence)
            if match_start_zero < 0:
                continue
            protein_start = match_start_zero + 1
            protein_end = protein_start + len(sequence) - 1
            key = (
                parsed["identity"].get("primary_accession"),
                parsed["identity"].get("uniprot_id"),
                protein_start,
                protein_end,
            )
            if key in seen:
                continue
            seen.add(key)
            matches.append({
                "evidence_kind": "local_exact_lmp_sequence_match",
                "source_path": str(xml_path),
                "sequence_source": source["source"],
                "protein_start": protein_start,
                "protein_end": protein_end,
                "identity": parsed["identity"],
                "overlapping_features": _overlapping_features(parsed["features"], protein_start, protein_end),
                "biological_comments": parsed["comments"][:8],
            })
            if len(matches) >= max_matches:
                return matches
    return matches


def _iter_lmp_xml_files(roots: Sequence[Path]) -> Iterable[Path]:
    for root in roots:
        if not root.exists():
            continue
        primary = sorted(root.rglob("*.lmp_v4.xml"))
        if primary:
            yield from primary
            continue
        yield from sorted(root.rglob("*.xml"))


def _parse_lmp_xml_for_sequences(xml_path: Path) -> Optional[Dict[str, Any]]:
    try:
        root = ET.parse(xml_path).getroot()
    except ET.ParseError:
        return None
    except OSError:
        return None

    identity = {
        "primary_accession": _find_first_text(root, "PrimaryAccession"),
        "isoform_accession": _find_first_text(root, "IsoformAccession"),
        "uniprot_id": _find_first_text(root, "UniProtKBId"),
        "organism": _find_first_text(root, "Organism"),
        "genes": _find_gene_values(root),
    }
    comments = [
        {"type": elem.get("type", ""), "text": (elem.text or "").strip()}
        for elem in root.iter()
        if _local_name(elem.tag) == "Comment" and (elem.text or "").strip()
    ]
    features: List[Dict[str, Any]] = []
    sequences: List[Dict[str, str]] = []

    for elem in root.iter():
        tag = _local_name(elem.tag)
        if tag in {"Domain", "Motif", "Region", "Site"}:
            feature = _feature_from_xml_element(elem, tag)
            if feature:
                features.append(feature)
        if tag == "Chain" and elem.get("sequence"):
            plain_sequence = _clean_sequence_text(elem.get("sequence", ""))
            if plain_sequence:
                sequences.append({"source": f"Chain:{elem.get('id', '')}", "plain_sequence": plain_sequence})
        if tag in {"NeSyGrammar", "Sequence"} and (elem.text or "").strip():
            plain_sequence, inline_features = _plain_sequence_and_annotations(elem.text or "")
            if plain_sequence:
                sequences.append({"source": tag, "plain_sequence": plain_sequence})
            features.extend(inline_features)

    return {
        "identity": identity,
        "comments": comments,
        "features": _dedupe_features(features),
        "sequences": sequences,
    }


def _feature_from_xml_element(elem: ET.Element, tag: str) -> Optional[Dict[str, Any]]:
    start = _parse_int(elem.get("start", ""))
    end = _parse_int(elem.get("end", ""))
    if start is None or end is None:
        return None
    return {
        "source": "lmp_xml_element",
        "type": elem.get("type", tag.lower()),
        "name": elem.get("name", elem.get("description", tag)),
        "start": start,
        "end": end,
        "interpro_id": elem.get("interpro_id", ""),
    }


def _plain_sequence_and_annotations(text: str) -> Tuple[str, List[Dict[str, Any]]]:
    plain: List[str] = []
    annotations: List[Dict[str, Any]] = []
    open_annotations: List[Dict[str, Any]] = []
    index = 0
    while index < len(text):
        match = _NESY_TAG_PATTERN.match(text, index)
        if match:
            kind = match.group("kind").lower()
            if match.group("close"):
                for stack_index in range(len(open_annotations) - 1, -1, -1):
                    if open_annotations[stack_index]["kind"] == kind:
                        annotation = open_annotations.pop(stack_index)
                        annotation["end"] = len(plain)
                        if annotation["end"] >= annotation["start"]:
                            annotations.append({
                                "source": "lmp_nesy_inline",
                                "type": annotation["kind"],
                                "name": annotation["name"],
                                "start": annotation["start"],
                                "end": annotation["end"],
                            })
                        break
            else:
                open_annotations.append({
                    "kind": kind,
                    "name": match.group("name") or kind,
                    "start": len(plain) + 1,
                })
            index = match.end()
            continue

        char = text[index].upper()
        if char in _AMINO_ACID_LETTERS:
            plain.append(char)
        index += 1
    return "".join(plain), annotations


def _find_first_text(root: ET.Element, local_name: str) -> str:
    for elem in root.iter():
        if _local_name(elem.tag) == local_name and elem.text:
            return elem.text.strip()
    return ""


def _find_gene_values(root: ET.Element) -> List[str]:
    genes: List[str] = []
    for genes_elem in root.iter():
        if _local_name(genes_elem.tag) != "Genes":
            continue
        for elem in list(genes_elem):
            if _local_name(elem.tag) == "Value" and elem.text:
                genes.append(elem.text.strip())
    return genes


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _clean_sequence_text(text: str) -> str:
    return "".join(char for char in text.upper() if char in _AMINO_ACID_LETTERS)


def _dedupe_features(features: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    deduped: List[Dict[str, Any]] = []
    seen = set()
    for feature in features:
        key = (feature.get("source"), feature.get("type"), feature.get("name"), feature.get("start"), feature.get("end"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(feature)
    return deduped


def _overlapping_features(features: Sequence[Dict[str, Any]], start: int, end: int) -> List[Dict[str, Any]]:
    overlaps: List[Dict[str, Any]] = []
    for feature in features:
        feature_start = _parse_int(str(feature.get("start", "")))
        feature_end = _parse_int(str(feature.get("end", "")))
        if feature_start is None or feature_end is None:
            continue
        if feature_end < start or feature_start > end:
            continue
        payload = dict(feature)
        payload["overlap_start"] = max(feature_start, start)
        payload["overlap_end"] = min(feature_end, end)
        payload["chain_local_start"] = payload["overlap_start"] - start + 1
        payload["chain_local_end"] = payload["overlap_end"] - start + 1
        overlaps.append(payload)
    return overlaps


def _resolve_remote_chain_identity(
    chain: PDBChainContext,
    *,
    timeout_seconds: int,
    enable_remote_mmseqs2: bool,
) -> Dict[str, Any]:
    resolution = _empty_remote_resolution(executed=True)
    resolution["services"] = ["rcsb_sequence_search", "rcsb_polymer_entity", "uniprot_rest"]
    if enable_remote_mmseqs2:
        resolution["services"].insert(0, "remote_mmseqs2")
        resolution["ncbi_blast"] = _run_mmseqs2_serverless(chain.sequence, chain.chain_id, timeout_seconds)

    try:
        hits = _query_rcsb_sequence(chain.sequence, timeout_seconds=timeout_seconds)
    except Exception as exc:  # pragma: no cover - network failure path
        resolution["errors"].append(f"RCSB sequence search failed: {exc}")
        hits = []

    for hit in hits[:5]:
        candidate = _build_rcsb_candidate(chain.sequence, hit, timeout_seconds=timeout_seconds)
        if candidate:
            resolution["candidate_hits"].append(candidate)

    blast_identity = _accepted_identity_from_mmseqs2(resolution["ncbi_blast"], chain.sequence, timeout_seconds)
    if blast_identity:
        resolution["accepted_identity"] = blast_identity
        return resolution

    for candidate in resolution["candidate_hits"]:
        score = float(candidate.get("score") or 0.0)
        uniprot = candidate.get("uniprot") or {}
        if score >= 0.9 and uniprot.get("accession"):
            resolution["accepted_identity"] = _accepted_identity_from_rcsb_candidate(candidate)
            return resolution
    return resolution


def _query_rcsb_sequence(sequence: str, *, timeout_seconds: int) -> List[Dict[str, Any]]:
    payload = {
        "query": {
            "type": "terminal",
            "service": "sequence",
            "parameters": {
                "evalue_cutoff": 10,
                "identity_cutoff": 0.5,
                "sequence_type": "protein",
                "target": "pdb_protein_sequence",
                "value": sequence,
            },
        },
        "request_options": {"paginate": {"start": 0, "rows": 10}},
        "return_type": "polymer_entity",
    }
    data = _fetch_json(_RCSB_SEQUENCE_SEARCH_URL, timeout_seconds=timeout_seconds, payload=payload)
    return list(data.get("result_set", []))


def _build_rcsb_candidate(sequence: str, hit: Dict[str, Any], *, timeout_seconds: int) -> Optional[Dict[str, Any]]:
    identifier = str(hit.get("identifier", ""))
    if "_" not in identifier:
        return None
    entry_id, entity_id = identifier.split("_", 1)
    entity_url = _RCSB_POLYMER_ENTITY_URL.format(entry_id=entry_id, entity_id=entity_id)
    try:
        entity = _fetch_json(entity_url, timeout_seconds=timeout_seconds)
    except Exception:
        return None

    container = entity.get("rcsb_polymer_entity_container_identifiers", {})
    reference_ids = container.get("reference_sequence_identifiers") or []
    uniprot_accession = ""
    if container.get("uniprot_ids"):
        uniprot_accession = container["uniprot_ids"][0]
    for reference in reference_ids:
        if reference.get("database_name") == "UniProt" and reference.get("database_accession"):
            uniprot_accession = reference["database_accession"]
            break

    entity_sequence = _clean_sequence_text(entity.get("entity_poly", {}).get("pdbx_seq_one_letter_code_can", ""))
    uniprot_record = _fetch_uniprot_record(uniprot_accession, timeout_seconds) if uniprot_accession else {}
    uniprot_sequence = uniprot_record.get("sequence", "")
    uniprot_exact_range = _find_sequence_range(uniprot_sequence, sequence)
    entity_exact_range = _find_sequence_range(entity_sequence, sequence)
    entity_inside_query = _find_sequence_range(sequence, entity_sequence) if entity_sequence else None
    uniprot_alignment = None if uniprot_exact_range else _align_sequence_to_source(uniprot_sequence, sequence)
    entity_alignment = None if entity_exact_range else _align_sequence_to_source(entity_sequence, sequence)
    uniprot_range = uniprot_exact_range or (uniprot_alignment or {}).get("source_range")
    entity_range = entity_exact_range or (entity_alignment or {}).get("source_range")

    features = _overlapping_features(uniprot_record.get("features", []), *uniprot_range) if uniprot_range else []
    return {
        "evidence_kind": "rcsb_sequence_search_hit",
        "identifier": identifier,
        "score": hit.get("score"),
        "entry_id": entry_id,
        "polymer_entity_id": entity_id,
        "title": entity.get("rcsb_polymer_entity", {}).get("pdbx_description", ""),
        "source_organism": _simplify_rcsb_source_organism(entity),
        "entity_poly": {
            "type": entity.get("entity_poly", {}).get("type", ""),
            "strand_id": entity.get("entity_poly", {}).get("pdbx_strand_id", ""),
            "sequence_length": len(entity_sequence),
        },
        "sifts_reference_sequences": reference_ids,
        "sequence_mapping": {
            "chain_exact_in_rcsb_entity": entity_exact_range is not None,
            "rcsb_entity_exact_in_chain": entity_inside_query is not None,
            "chain_exact_in_uniprot": uniprot_exact_range is not None,
            "rcsb_entity_range": entity_range,
            "uniprot_range": uniprot_range,
            "rcsb_entity_alignment": entity_alignment,
            "uniprot_alignment": uniprot_alignment,
        },
        "uniprot": uniprot_record,
        "overlapping_uniprot_features": features,
    }


def _fetch_uniprot_record(accession: str, timeout_seconds: int) -> Dict[str, Any]:
    if not accession:
        return {}
    try:
        data = _fetch_json(_UNIPROT_JSON_URL.format(accession=accession), timeout_seconds=timeout_seconds)
    except Exception:
        return {"accession": accession, "features": [], "sequence": ""}
    return _parse_uniprot_record(data)


def _fetch_json(url: str, *, timeout_seconds: int, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    body = None
    headers = {"User-Agent": "MICA-LMP-StructureAssetContext/1.0"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def _parse_uniprot_record(data: Dict[str, Any]) -> Dict[str, Any]:
    accession = data.get("primaryAccession", "")
    sequence = _clean_sequence_text(data.get("sequence", {}).get("value", ""))
    features = [_feature_from_uniprot(feature) for feature in data.get("features", [])]
    comments = []
    for comment in data.get("comments", []):
        comment_type = comment.get("commentType", "")
        texts = [item.get("value", "") for item in comment.get("texts", []) if item.get("value")]
        if texts:
            comments.append({"type": comment_type, "text": " ".join(texts)})
    return {
        "accession": accession,
        "entry_name": data.get("uniProtkbId", ""),
        "protein_name": _uniprot_protein_name(data),
        "genes": _uniprot_gene_names(data),
        "organism": data.get("organism", {}).get("scientificName", ""),
        "taxon_id": data.get("organism", {}).get("taxonId"),
        "sequence_length": len(sequence),
        "sequence": sequence,
        "features": [feature for feature in features if feature],
        "comments": comments[:8],
    }


def _feature_from_uniprot(feature: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    location = feature.get("location", {})
    start = _parse_int(str(location.get("start", {}).get("value", "")))
    end = _parse_int(str(location.get("end", {}).get("value", "")))
    if start is None or end is None:
        return None
    return {
        "source": "uniprot_feature",
        "type": feature.get("type", ""),
        "name": feature.get("description", feature.get("type", "")),
        "start": start,
        "end": end,
        "feature_id": feature.get("featureId", ""),
    }


def _uniprot_protein_name(data: Dict[str, Any]) -> str:
    description = data.get("proteinDescription", {})
    recommended = description.get("recommendedName", {})
    full_name = recommended.get("fullName", {}).get("value")
    if full_name:
        return full_name
    for submitted in description.get("submissionNames", []):
        full_name = submitted.get("fullName", {}).get("value")
        if full_name:
            return full_name
    return ""


def _uniprot_gene_names(data: Dict[str, Any]) -> List[str]:
    names: List[str] = []
    for gene in data.get("genes", []):
        for key in ("geneName", "orderedLocusNames", "orfNames"):
            value = gene.get(key)
            if isinstance(value, dict) and value.get("value"):
                names.append(value["value"])
            elif isinstance(value, list):
                names.extend(item.get("value", "") for item in value if item.get("value"))
    return [name for index, name in enumerate(names) if name and name not in names[:index]]


def _simplify_rcsb_source_organism(entity: Dict[str, Any]) -> Dict[str, Any]:
    sources = entity.get("rcsb_entity_source_organism") or []
    if not sources:
        return {}
    source = sources[0]
    genes = []
    for gene in source.get("rcsb_gene_name", []) or []:
        if gene.get("value"):
            genes.append(gene["value"])
    return {
        "scientific_name": source.get("scientific_name") or source.get("ncbi_scientific_name", ""),
        "common_name": source.get("common_name", ""),
        "taxon_id": source.get("ncbi_taxonomy_id"),
        "source_type": source.get("source_type", ""),
        "genes": genes,
    }


def _find_sequence_range(source_sequence: str, query_sequence: str) -> Optional[List[int]]:
    if not source_sequence or not query_sequence:
        return None
    index = source_sequence.find(query_sequence)
    if index < 0:
        return None
    start = index + 1
    return [start, start + len(query_sequence) - 1]


def _align_sequence_to_source(source_sequence: str, query_sequence: str) -> Optional[Dict[str, Any]]:
    if not source_sequence or not query_sequence:
        return None
    try:
        from Bio.Align import PairwiseAligner
    except ImportError:
        return None

    aligner = PairwiseAligner()
    aligner.mode = "local"
    aligner.match_score = 2.0
    aligner.mismatch_score = -1.0
    aligner.open_gap_score = -5.0
    aligner.extend_gap_score = -1.0
    alignments = aligner.align(source_sequence, query_sequence)
    if not alignments:
        return None
    alignment = alignments[0]
    target_blocks, query_blocks = alignment.aligned
    if len(target_blocks) == 0 or len(query_blocks) == 0:
        return None

    query_aligned = 0
    matches = 0
    aligned_blocks = []
    for target_block, query_block in zip(target_blocks, query_blocks):
        target_start, target_end = int(target_block[0]), int(target_block[1])
        query_start, query_end = int(query_block[0]), int(query_block[1])
        query_aligned += query_end - query_start
        target_fragment = source_sequence[target_start:target_end]
        query_fragment = query_sequence[query_start:query_end]
        matches += sum(1 for source_residue, query_residue in zip(target_fragment, query_fragment) if source_residue == query_residue)
        aligned_blocks.append(
            {
                "source_range": [target_start + 1, target_end],
                "query_range": [query_start + 1, query_end],
            }
        )

    query_coverage = query_aligned / len(query_sequence)
    identity = matches / query_aligned if query_aligned else 0.0
    if query_coverage < 0.8 or identity < 0.7:
        return None

    return {
        "source_range": [int(target_blocks[0][0]) + 1, int(target_blocks[-1][1])],
        "query_range": [int(query_blocks[0][0]) + 1, int(query_blocks[-1][1])],
        "query_coverage": round(query_coverage, 4),
        "identity": round(identity, 4),
        "score": round(float(alignment.score), 4),
        "aligned_blocks": aligned_blocks,
    }


def _accepted_identity_from_rcsb_candidate(candidate: Dict[str, Any]) -> Dict[str, Any]:
    uniprot = candidate.get("uniprot") or {}
    mapping = candidate.get("sequence_mapping") or {}
    evidence_chain = ["rcsb_sequence_search", "rcsb_polymer_entity", "sifts_uniprot_reference", "uniprot_rest"]
    confidence = "high" if mapping.get("chain_exact_in_uniprot") else "medium"
    return {
        "identity_source": "remote_rcsb_sifts_uniprot",
        "confidence": confidence,
        "evidence_chain": evidence_chain,
        "uniprot_accession": uniprot.get("accession", ""),
        "uniprot_entry_name": uniprot.get("entry_name", ""),
        "protein_name": uniprot.get("protein_name") or candidate.get("title", ""),
        "genes": uniprot.get("genes", []) or candidate.get("source_organism", {}).get("genes", []),
        "organism": uniprot.get("organism") or candidate.get("source_organism", {}).get("scientific_name", ""),
        "taxon_id": uniprot.get("taxon_id") or candidate.get("source_organism", {}).get("taxon_id"),
        "protein_range": mapping.get("uniprot_range"),
        "pdb_hit": {
            "entry_id": candidate.get("entry_id"),
            "polymer_entity_id": candidate.get("polymer_entity_id"),
            "identifier": candidate.get("identifier"),
            "score": candidate.get("score"),
            "title": candidate.get("title"),
        },
        "domains_and_features": candidate.get("overlapping_uniprot_features", []),
        "uniprot_comments": uniprot.get("comments", []),
    }


def _run_mmseqs2_serverless(sequence: str, chain_id: str, timeout_seconds: int) -> Dict[str, Any]:
    try:
        from bsm.alignment.blast_integration import BlastConfig, BlastService
    except Exception as exc:
        return {"executed": False, "hits": [], "errors": [f"BlastService import failed: {exc}"]}

    try:
        config = BlastConfig(
            use_remote=True,
            timeout_seconds=timeout_seconds,
        )
        service = BlastService(config)
        
        # Try to run in current loop or create a loop if not present
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import nest_asyncio
            nest_asyncio.apply()

        result = asyncio.run(service.search(
            sequence=sequence,
            query_id=f"chain_{chain_id}",
        ))
    except RuntimeError as exc:
        try:
            result = asyncio.get_event_loop().run_until_complete(service.search(
                sequence=sequence,
                query_id=f"chain_{chain_id}",
            ))
        except Exception as inner_exc:
            return {"executed": True, "hits": [], "errors": [f"MMseqs2 runtime error: {exc} / {inner_exc}"]}
    except Exception as exc:
        return {"executed": True, "hits": [], "errors": [f"MMseqs2 error: {exc}"]}

    return {
        "executed": True,
        "database": result.database_used,
        "program": result.program_used,
        "num_hits": result.num_hits,
        "hits": [hit.to_dict() for hit in result.hits[:5]],
        "errors": result.errors,
        "warnings": result.warnings,
    }


def _accepted_identity_from_mmseqs2(
    mmseqs_payload: Dict[str, Any],
    sequence: str,
    timeout_seconds: int,
) -> Optional[Dict[str, Any]]:
    if not mmseqs_payload.get("executed") or mmseqs_payload.get("errors"):
        return None
    hits = mmseqs_payload.get("hits", [])
    if not hits:
        return None
    best_hit = hits[0]
    accession = _parse_uniprot_accession_from_blast_hit(best_hit)
    if not accession:
        return None
    uniprot = _fetch_uniprot_record(accession, timeout_seconds)
    protein_range = _find_sequence_range(uniprot.get("sequence", ""), sequence)
    if not protein_range:
        return None
    return {
        "identity_source": "remote_mmseqs2_uniprot",
        "confidence": "high" if float(best_hit.get("identity", 0.0)) >= 95.0 else "medium",
        "evidence_chain": ["BlastService", "MMseqsService", "MMSEQS_ENDPOINT_URL", "UniProt REST"],
        "uniprot_accession": uniprot.get("accession", accession),
        "uniprot_entry_name": uniprot.get("entry_name", ""),
        "protein_name": uniprot.get("protein_name", ""),
        "genes": uniprot.get("genes", []),
        "organism": uniprot.get("organism", ""),
        "taxon_id": uniprot.get("taxon_id"),
        "protein_range": protein_range,
        "blast_best_hit": {
            "subject_id": best_hit.get("subject_id"),
            "identity": best_hit.get("identity"),
            "query_coverage": best_hit.get("query_coverage"),
            "alignment_length": best_hit.get("alignment_length"),
            "e_value": best_hit.get("e_value"),
            "bit_score": best_hit.get("bit_score"),
            "subject_title": best_hit.get("subject_title"),
        },
        "domains_and_features": _overlapping_features(uniprot.get("features", []), protein_range[0], protein_range[1]),
        "uniprot_comments": uniprot.get("comments", []),
    }


def _run_ncbi_remote_blast(sequence: str, chain_id: str, timeout_seconds: int) -> Dict[str, Any]:
    try:
        from bsm.alignment.blast_integration import BlastConfig, BlastDatabase, BlastProgram, BlastService
    except Exception as exc:
        return {"executed": False, "hits": [], "errors": [f"BlastService import failed: {exc}"]}

    try:
        config = BlastConfig(
            program=BlastProgram.BLASTP,
            database=BlastDatabase.SWISSPROT,
            use_remote=True,
            max_target_seqs=5,
            timeout_seconds=timeout_seconds,
        )
        service = BlastService(config)
        result = asyncio.run(service.search(
            sequence=sequence,
            query_id=f"chain_{chain_id}",
            program=BlastProgram.BLASTP,
            database=BlastDatabase.SWISSPROT.value,
            evalue=1e-3,
            max_hits=5,
        ))
    except RuntimeError as exc:
        return {"executed": True, "hits": [], "errors": [f"Remote BLAST runtime error: {exc}"]}
    except Exception as exc:
        return {"executed": True, "hits": [], "errors": [f"Remote BLAST error: {exc}"]}

    return {
        "executed": True,
        "database": result.database_used,
        "program": result.program_used,
        "num_hits": result.num_hits,
        "hits": [hit.to_dict() for hit in result.hits[:5]],
        "errors": result.errors,
        "warnings": result.warnings,
    }


def _accepted_identity_from_ncbi_blast(
    blast_payload: Dict[str, Any],
    sequence: str,
    timeout_seconds: int,
) -> Optional[Dict[str, Any]]:
    if not blast_payload.get("executed") or blast_payload.get("errors"):
        return None
    hits = blast_payload.get("hits", [])
    if not hits:
        return None
    best_hit = hits[0]
    accession = _parse_uniprot_accession_from_blast_hit(best_hit)
    if not accession:
        return None
    uniprot = _fetch_uniprot_record(accession, timeout_seconds)
    protein_range = _find_sequence_range(uniprot.get("sequence", ""), sequence)
    if not protein_range:
        return None
    return {
        "identity_source": "ncbi_remote_blast_uniprot",
        "confidence": "high" if float(best_hit.get("identity", 0.0)) >= 95.0 else "medium",
        "evidence_chain": ["BlastService", "NCBIWWW.qblast", "NCBI_API_KEY", "UniProt REST"],
        "uniprot_accession": uniprot.get("accession", accession),
        "uniprot_entry_name": uniprot.get("entry_name", ""),
        "protein_name": uniprot.get("protein_name", ""),
        "genes": uniprot.get("genes", []),
        "organism": uniprot.get("organism", ""),
        "taxon_id": uniprot.get("taxon_id"),
        "protein_range": protein_range,
        "blast_best_hit": {
            "subject_id": best_hit.get("subject_id"),
            "identity": best_hit.get("identity"),
            "query_coverage": best_hit.get("query_coverage"),
            "alignment_length": best_hit.get("alignment_length"),
            "e_value": best_hit.get("e_value"),
            "bit_score": best_hit.get("bit_score"),
            "subject_title": best_hit.get("subject_title"),
        },
        "domains_and_features": _overlapping_features(uniprot.get("features", []), protein_range[0], protein_range[1]),
        "uniprot_comments": uniprot.get("comments", []),
    }


def _parse_uniprot_accession_from_blast_hit(hit: Dict[str, Any]) -> str:
    subject_id = str(hit.get("subject_id", ""))
    subject_title = str(hit.get("subject_title", ""))
    for text in (subject_id, subject_title):
        match = re.search(r"\b(?:sp|tr)\|([A-Z0-9]+)\|", text)
        if match:
            return match.group(1)
    match = re.search(r"\b([OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9][A-Z][A-Z0-9]{2}[0-9])\b", subject_id + " " + subject_title)
    return match.group(1) if match else ""


def _select_accepted_identity(
    chain: PDBChainContext,
    *,
    metadata_hints: Sequence[Dict[str, Any]],
    local_matches: Sequence[Dict[str, Any]],
    remote_resolution: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    if remote_resolution.get("accepted_identity"):
        return remote_resolution["accepted_identity"]
    if local_matches:
        local_match = local_matches[0]
        identity = local_match.get("identity", {})
        return {
            "identity_source": "local_lmp_exact_sequence_match",
            "confidence": "high",
            "evidence_chain": ["local_lmp_xml", "exact_sequence_substring"],
            "uniprot_accession": identity.get("primary_accession") or identity.get("uniprot_id"),
            "isoform_accession": identity.get("isoform_accession", ""),
            "uniprot_entry_name": identity.get("uniprot_id", ""),
            "protein_name": "; ".join(identity.get("genes", [])[:2]),
            "genes": identity.get("genes", []),
            "organism": identity.get("organism", ""),
            "protein_range": [local_match.get("protein_start"), local_match.get("protein_end")],
            "domains_and_features": local_match.get("overlapping_features", []),
            "uniprot_comments": local_match.get("biological_comments", []),
        }
    if metadata_hints:
        hint = metadata_hints[0]
        return {
            "identity_source": "pdb_dbref_metadata",
            "confidence": "metadata_only",
            "evidence_chain": ["pdb_dbref"],
            "uniprot_accession": hint.get("accession", ""),
            "uniprot_entry_name": hint.get("database_id", ""),
            "protein_name": hint.get("database_id", ""),
            "genes": [],
            "organism": "",
            "protein_range": hint.get("database_range"),
            "domains_and_features": [],
            "uniprot_comments": [],
        }
    return None


def _collect_chain_domain_context(
    accepted_identity: Optional[Dict[str, Any]],
    local_matches: Sequence[Dict[str, Any]],
    remote_resolution: Dict[str, Any],
) -> Dict[str, Any]:
    features: List[Dict[str, Any]] = []
    if accepted_identity:
        features.extend(accepted_identity.get("domains_and_features", []))
    for match in local_matches:
        features.extend(match.get("overlapping_features", []))
    for candidate in remote_resolution.get("candidate_hits", []):
        features.extend(candidate.get("overlapping_uniprot_features", []))
    features = _dedupe_features(features)
    return {
        "feature_count": len(features),
        "features": features,
        "status": "features_mapped" if features else "no_overlapping_features_detected",
    }


def _build_biological_context(identity_resolution: Dict[str, Any], chains: List[PDBChainContext]) -> Dict[str, Any]:
    resolved_entities = []
    unresolved_chains = []
    for resolution in identity_resolution.get("chain_resolutions", []):
        accepted = resolution.get("accepted_identity")
        if accepted:
            resolved_entities.append({
                "chain_id": resolution.get("chain_id"),
                "uniprot_accession": accepted.get("uniprot_accession"),
                "entry_name": accepted.get("uniprot_entry_name"),
                "protein_name": accepted.get("protein_name"),
                "genes": accepted.get("genes", []),
                "organism": accepted.get("organism"),
                "protein_range": accepted.get("protein_range"),
                "source": accepted.get("identity_source"),
                "confidence": accepted.get("confidence"),
            })
        else:
            unresolved_chains.append(resolution.get("chain_id"))
    accessions = {entity.get("uniprot_accession") for entity in resolved_entities if entity.get("uniprot_accession")}
    organisms = {entity.get("organism") for entity in resolved_entities if entity.get("organism")}
    return {
        "context_schema": "mica.lmp.biological_context.v1",
        "status": identity_resolution.get("status"),
        "resolved_entities": resolved_entities,
        "unresolved_chains": unresolved_chains,
        "unique_accession_count": len(accessions),
        "organisms": sorted(organisms),
        "interpretation_boundary": "This payload reports evidence-derived identity, domains, and ranges only; pathway-level interpretation must cite downstream literature or curated LMP comments.",
        "chain_sequence_lengths": {chain.chain_id: len(chain.sequence) for chain in chains},
    }


def _build_physical_context(
    mutable_chains: Dict[str, _MutableChain],
    chains: List[PDBChainContext],
    *,
    heavy_atom_cutoff: float = 5.0,
    ca_cutoff: float = 8.0,
) -> Dict[str, Any]:
    if len(chains) < 2:
        return {
            "status": "insufficient_chains",
            "analysis_kind": "local_static_contact_analysis",
            "chain_pairs": [],
            "has_inter_chain_contacts": False,
            "cutoffs_angstrom": {"heavy_atom": heavy_atom_cutoff, "ca": ca_cutoff},
        }
    chain_pairs: List[Dict[str, Any]] = []
    chain_ids = [chain.chain_id for chain in chains]
    for index, chain_a in enumerate(chain_ids):
        for chain_b in chain_ids[index + 1:]:
            atoms_a = mutable_chains[chain_a].atoms
            atoms_b = mutable_chains[chain_b].atoms
            heavy_contacts = _calculate_contact_summary(
                [atom for atom in atoms_a if not atom.is_hydrogen()],
                [atom for atom in atoms_b if not atom.is_hydrogen()],
                cutoff=heavy_atom_cutoff,
                contact_kind="heavy_atom",
            )
            ca_contacts = _calculate_contact_summary(
                [atom for atom in atoms_a if atom.atom_name.strip().upper() == "CA"],
                [atom for atom in atoms_b if atom.atom_name.strip().upper() == "CA"],
                cutoff=ca_cutoff,
                contact_kind="ca",
            )
            chain_pairs.append({
                "chain_a": chain_a,
                "chain_b": chain_b,
                "heavy_atom_contacts": heavy_contacts,
                "ca_contacts": ca_contacts,
                "interface_detected": heavy_contacts["atom_contact_count"] > 0 or ca_contacts["atom_contact_count"] > 0,
            })
    return {
        "status": "contacts_computed",
        "analysis_kind": "local_static_contact_analysis",
        "smic_compatibility": "static_zero_frame_contacts_aligned_with_smic_general_interaction_semantics",
        "trajectory_executed": False,
        "cutoffs_angstrom": {"heavy_atom": heavy_atom_cutoff, "ca": ca_cutoff},
        "chain_pairs": chain_pairs,
        "has_inter_chain_contacts": any(pair["interface_detected"] for pair in chain_pairs),
    }


def _calculate_contact_summary(
    atoms_a: Sequence[PDBAtomObservation],
    atoms_b: Sequence[PDBAtomObservation],
    *,
    cutoff: float,
    contact_kind: str,
    max_reported_contacts: int = 20,
) -> Dict[str, Any]:
    atom_contact_count = 0
    residue_pairs: Dict[Tuple[str, str], float] = {}
    residue_counts: Dict[str, int] = {}
    closest_contacts: List[Dict[str, Any]] = []
    cutoff_squared = cutoff * cutoff
    for atom_a in atoms_a:
        for atom_b in atoms_b:
            distance_squared = _distance_squared(atom_a, atom_b)
            if distance_squared > cutoff_squared:
                continue
            distance = math.sqrt(distance_squared)
            atom_contact_count += 1
            residue_pair = (atom_a.residue_label(), atom_b.residue_label())
            if residue_pair not in residue_pairs or distance < residue_pairs[residue_pair]:
                residue_pairs[residue_pair] = distance
            residue_counts[atom_a.residue_label()] = residue_counts.get(atom_a.residue_label(), 0) + 1
            residue_counts[atom_b.residue_label()] = residue_counts.get(atom_b.residue_label(), 0) + 1
            _append_closest_contact(closest_contacts, atom_a, atom_b, distance, max_reported_contacts)
    closest_contacts.sort(key=lambda item: item["distance_angstrom"])
    interface_residues = sorted(
        ({"residue": residue, "atom_contact_count": count} for residue, count in residue_counts.items()),
        key=lambda item: item["atom_contact_count"],
        reverse=True,
    )[:20]
    return {
        "contact_kind": contact_kind,
        "cutoff_angstrom": cutoff,
        "atom_contact_count": atom_contact_count,
        "unique_residue_pair_count": len(residue_pairs),
        "minimum_distance_angstrom": round(min(residue_pairs.values()), 3) if residue_pairs else None,
        "interface_residues": interface_residues,
        "closest_contacts": closest_contacts,
    }


def _distance_squared(atom_a: PDBAtomObservation, atom_b: PDBAtomObservation) -> float:
    return (
        (atom_a.x - atom_b.x) ** 2
        + (atom_a.y - atom_b.y) ** 2
        + (atom_a.z - atom_b.z) ** 2
    )


def _append_closest_contact(
    contacts: List[Dict[str, Any]],
    atom_a: PDBAtomObservation,
    atom_b: PDBAtomObservation,
    distance: float,
    max_contacts: int,
) -> None:
    payload = {
        "chain_a_residue": atom_a.residue_label(),
        "chain_a_atom": atom_a.atom_name,
        "chain_b_residue": atom_b.residue_label(),
        "chain_b_atom": atom_b.atom_name,
        "distance_angstrom": round(distance, 3),
    }
    if len(contacts) < max_contacts:
        contacts.append(payload)
        return
    farthest_index, farthest = max(enumerate(contacts), key=lambda item: item[1]["distance_angstrom"])
    if payload["distance_angstrom"] < farthest["distance_angstrom"]:
        contacts[farthest_index] = payload


def _parse_int(value: str) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_float(value: str) -> Optional[float]:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


__all__ = [
    "AMINO_ACID_3_TO_1",
    "PDBChainContext",
    "PDBResidueObservation",
    "PDBStructureAssetContext",
    "StructureAssetRecord",
    "detect_pdb_structure_context",
]