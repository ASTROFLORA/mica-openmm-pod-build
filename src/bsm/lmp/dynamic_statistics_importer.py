from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

DEFAULT_DATASET_NAME = "protein_dynamic_properties"
DEFAULT_SOURCE_KIND = "dataset_bridge"

_WRAPPER_KEYS = ("protein_dynamic_properties", "dynamic_properties")
_RECEIPT_KEYS = {
    "source_kind",
    "run_metadata",
    "runs",
    "dataset_refs",
    "datasets",
    "residue_stats",
    "residue_dynamic_stats",
    "pair_stats",
    "pair_dynamic_stats",
}
_PAIR_KEY_RE = re.compile(r"(?P<left>\d+)\s*[-,:_/]\s*(?P<right>\d+)")


def normalize_dynamic_statistics_receipt(
    raw: Any,
    *,
    default_dataset: str = DEFAULT_DATASET_NAME,
    default_source_kind: str = DEFAULT_SOURCE_KIND,
) -> Dict[str, Any]:
    payload = _coerce_mapping(raw)
    if not payload:
        return {}

    payload = _unwrap_payload(payload)
    context = _context_payload(payload)
    residue_stats = _normalize_residue_stats(context)
    pair_stats = _normalize_pair_stats(context)
    normalized = {
        "source_kind": str(context.get("source_kind") or default_source_kind).strip() or default_source_kind,
        "run_metadata": _normalize_run_metadata(context),
        "dataset_refs": _normalize_dataset_refs(
            context,
            default_dataset=default_dataset,
            has_stats=bool(residue_stats or pair_stats),
        ),
        "residue_stats": residue_stats,
        "pair_stats": pair_stats,
    }
    if not any(
        (
            normalized["run_metadata"],
            normalized["dataset_refs"],
            normalized["residue_stats"],
            normalized["pair_stats"],
        )
    ):
        return {}
    return normalized


def _coerce_mapping(raw: Any) -> Dict[str, Any]:
    return dict(raw) if isinstance(raw, Mapping) else {}


def _context_payload(payload: Mapping[str, Any]) -> Dict[str, Any]:
    context = dict(payload)
    metadata = payload.get("metadata")
    if isinstance(metadata, Mapping):
        for key, value in metadata.items():
            context.setdefault(str(key), value)
    return context


def _unwrap_payload(payload: Mapping[str, Any]) -> Dict[str, Any]:
    if any(key in payload for key in _RECEIPT_KEYS):
        return dict(payload)
    for wrapper_key in _WRAPPER_KEYS:
        wrapped = payload.get(wrapper_key)
        if not isinstance(wrapped, Mapping):
            continue
        merged = dict(wrapped)
        for key, value in payload.items():
            if key == wrapper_key:
                continue
            merged.setdefault(str(key), value)
        return merged
    return dict(payload)


def _normalize_run_metadata(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for raw_record in _iter_mapping_records(payload.get("run_metadata") or payload.get("runs")):
        record = _normalize_run_record(raw_record)
        if record:
            normalized.append(record)
    if normalized:
        return normalized
    derived = _normalize_run_record(payload)
    return [derived] if derived else []


def _normalize_run_record(record: Mapping[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for field, aliases, caster in (
        ("run_id", ("run_id", "dynamic_run_id"), _as_string),
        ("engine", ("engine", "md_engine"), _as_string),
        ("topology_ref", ("topology_ref", "topology", "topology_id"), _as_string),
        ("trajectory_ref", ("trajectory_ref", "trajectory", "trajectory_id"), _as_string),
        ("replica_id", ("replica_id", "replica", "replicate_id"), _as_string),
        ("replica_count", ("replica_count",), _as_int),
        ("ensemble_id", ("ensemble_id",), _as_string),
        ("force_field", ("force_field",), _as_string),
        ("solvent_model", ("solvent_model",), _as_string),
        ("n_frames", ("n_frames", "frames"), _as_int),
        ("stride", ("stride",), _as_int),
        ("time_step_ps", ("time_step_ps", "dt_ps"), _as_float),
        ("duration_ns", ("duration_ns", "simulation_ns"), _as_float),
        ("temperature_k", ("temperature_k", "temperature"), _as_float),
    ):
        value = _first_cast_value(record, aliases, caster)
        if value is not None:
            payload[field] = value
    return payload


def _normalize_dataset_refs(
    payload: Mapping[str, Any],
    *,
    default_dataset: str,
    has_stats: bool,
) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for raw_record in _iter_mapping_records(payload.get("dataset_refs") or payload.get("datasets")):
        record = _normalize_dataset_record(raw_record, payload, default_dataset=default_dataset, has_stats=has_stats)
        if record:
            normalized.append(record)
    if normalized:
        return normalized
    derived = _normalize_dataset_record(payload, payload, default_dataset=default_dataset, has_stats=has_stats)
    return [derived] if derived else []


def _normalize_dataset_record(
    record: Mapping[str, Any],
    context: Mapping[str, Any],
    *,
    default_dataset: str,
    has_stats: bool,
) -> Dict[str, Any]:
    dataset = _first_cast_value(record, ("dataset", "dataset_name"), _as_string)
    if dataset is None:
        dataset = _first_cast_value(context, ("dataset", "dataset_name"), _as_string)
    record_id = _first_cast_value(
        record,
        (
            "record_id",
            "dynamic_dataset_ref",
            "accession",
            "uniprot_id",
            "uniprot",
            "entry_id",
            "protein_id",
        ),
        _as_string,
    )
    if record_id is None:
        record_id = _first_cast_value(
            context,
            (
                "record_id",
                "dynamic_dataset_ref",
                "accession",
                "uniprot_id",
                "uniprot",
                "entry_id",
                "protein_id",
            ),
            _as_string,
        )
    split = _first_cast_value(record, ("split", "dataset_split"), _as_string)
    if split is None:
        split = _first_cast_value(context, ("split", "dataset_split"), _as_string)
    source_uri = _first_cast_value(record, ("source_uri", "dataset_uri", "uri"), _as_string)
    if source_uri is None:
        source_uri = _first_cast_value(context, ("source_uri", "dataset_uri", "uri"), _as_string)
    if source_uri is None:
        receipt_hash = _first_cast_value(record, ("receipt_hash",), _as_string)
        if receipt_hash is None:
            receipt_hash = _first_cast_value(context, ("receipt_hash",), _as_string)
        if receipt_hash is not None:
            source_uri = f"receipt_hash:{receipt_hash}"

    if dataset is None and (record_id is not None or split is not None or source_uri is not None or has_stats):
        dataset = default_dataset
    if dataset is None:
        return {}

    payload_out: Dict[str, Any] = {"dataset": dataset}
    if record_id is not None:
        payload_out["record_id"] = record_id
    if split is not None:
        payload_out["split"] = split
    if source_uri is not None:
        payload_out["source_uri"] = source_uri
    return payload_out


def _normalize_residue_stats(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    raw_records = payload.get("residue_stats") or payload.get("residue_dynamic_stats") or payload.get("residues")
    for raw_record in _iter_records(raw_records, kind="residue"):
        record = _normalize_residue_record(raw_record)
        if record:
            normalized.append(record)
    return normalized


def _normalize_residue_record(record: Mapping[str, Any]) -> Dict[str, Any]:
    position = _first_cast_value(
        record,
        ("position", "residue_position", "residue_index", "residue_id", "residue_number", "seq_position"),
        _as_positive_int,
    )
    if position is None:
        return {}

    payload: Dict[str, Any] = {"position": position}
    chain = _first_cast_value(record, ("chain", "chain_id", "auth_chain_id", "label_chain_id"), _as_string)
    if chain is not None:
        payload["chain"] = chain
    for field, aliases in (
        ("rmsf", ("rmsf",)),
        ("sasa_mean", ("sasa_mean", "sasa_avg", "sasa_average")),
        ("sasa_std", ("sasa_std", "sasa_sd", "sasa_stddev", "sasa_stdev")),
        ("normal_mode_low", ("normal_mode_low", "nma_low")),
        ("normal_mode_mid", ("normal_mode_mid", "nma_mid")),
        ("normal_mode_high", ("normal_mode_high", "nma_high")),
    ):
        value = _first_cast_value(record, aliases, _as_float)
        if value is None:
            value = _nested_channel(record, field)
        if value is not None:
            payload[field] = value
    secondary_structure = _first_cast_value(
        record,
        ("secondary_structure", "dssp", "dssp_bin", "dssp_state"),
        _as_string,
    )
    if secondary_structure is not None:
        payload["secondary_structure"] = secondary_structure
    return payload


def _normalize_pair_stats(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    raw_records = payload.get("pair_stats") or payload.get("pair_dynamic_stats") or payload.get("pairs")
    for raw_record in _iter_records(raw_records, kind="pair"):
        record = _normalize_pair_record(raw_record)
        if record:
            normalized.append(record)
    return normalized


def _normalize_pair_record(record: Mapping[str, Any]) -> Dict[str, Any]:
    position_i = _first_cast_value(
        record,
        ("position_i", "residue_i", "residue_id_i", "residue_position_i"),
        _as_positive_int,
    )
    position_j = _first_cast_value(
        record,
        ("position_j", "residue_j", "residue_id_j", "residue_position_j"),
        _as_positive_int,
    )
    if position_i is None or position_j is None:
        return {}

    payload: Dict[str, Any] = {
        "position_i": position_i,
        "position_j": position_j,
    }
    chain_i = _first_cast_value(record, ("chain_i", "chain_id_i", "auth_chain_i", "label_chain_i"), _as_string)
    chain_j = _first_cast_value(record, ("chain_j", "chain_id_j", "auth_chain_j", "label_chain_j"), _as_string)
    if chain_i is not None:
        payload["chain_i"] = chain_i
    if chain_j is not None:
        payload["chain_j"] = chain_j
    for field, aliases in (
        ("vdw", ("vdw", "van_der_waals")),
        ("hbbb", ("hbbb", "hb_backbone_backbone")),
        ("hbsb", ("hbsb", "hb_sidechain_backbone")),
        ("hbss", ("hbss", "hb_sidechain_sidechain")),
        ("hydrophobic", ("hydrophobic",)),
        ("salt_bridge", ("salt_bridge", "saltbridge")),
        ("pi_cation", ("pi_cation", "pi-cation")),
        ("pi_stacking", ("pi_stacking", "pi_stack", "pi-stacking")),
        ("t_stacking", ("t_stacking", "t_stack", "t-stacking")),
        ("motion_correlation", ("motion_correlation", "correlation")),
        ("normal_mode_low", ("normal_mode_low", "nma_low")),
        ("normal_mode_mid", ("normal_mode_mid", "nma_mid")),
        ("normal_mode_high", ("normal_mode_high", "nma_high")),
    ):
        value = _first_cast_value(record, aliases, _as_float)
        if value is None:
            value = _nested_channel(record, field)
        if value is not None:
            payload[field] = value
    return payload


def _iter_mapping_records(raw: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(raw, Mapping):
        for value in raw.values():
            if isinstance(value, Mapping):
                yield value
        return
    if isinstance(raw, (list, tuple)):
        for value in raw:
            if isinstance(value, Mapping):
                yield value


def _iter_records(raw: Any, *, kind: str) -> Iterable[Dict[str, Any]]:
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            if not isinstance(value, Mapping):
                continue
            payload = dict(value)
            if kind == "residue":
                payload.setdefault("position", key)
            else:
                left, right = _parse_pair_key(key)
                if left is not None and right is not None:
                    payload.setdefault("position_i", left)
                    payload.setdefault("position_j", right)
            yield payload
        return
    if isinstance(raw, (list, tuple)):
        for value in raw:
            if isinstance(value, Mapping):
                yield dict(value)


def _parse_pair_key(raw: Any) -> Tuple[Optional[int], Optional[int]]:
    if isinstance(raw, (tuple, list)) and len(raw) == 2:
        left = _as_positive_int(raw[0])
        right = _as_positive_int(raw[1])
        return left, right
    if not isinstance(raw, str):
        return None, None
    match = _PAIR_KEY_RE.search(raw)
    if match is None:
        return None, None
    return _as_positive_int(match.group("left")), _as_positive_int(match.group("right"))


def _nested_channel(record: Mapping[str, Any], field: str) -> Optional[float]:
    alias = field.replace("normal_mode_", "")
    for key in ("normal_modes", "nma"):
        nested = record.get(key)
        if not isinstance(nested, Mapping):
            continue
        value = nested.get(alias)
        cast = _as_float(value)
        if cast is not None:
            return cast
    return None


def _first_cast_value(
    payload: Mapping[str, Any],
    keys: Iterable[str],
    caster,
) -> Any:
    for key in keys:
        if key not in payload:
            continue
        value = caster(payload.get(key))
        if value is not None:
            return value
    return None


def _as_string(value: Any) -> Optional[str]:
    if value in {None, ""}:
        return None
    text = str(value).strip()
    return text or None


def _as_int(value: Any) -> Optional[int]:
    if value in {None, ""}:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_positive_int(value: Any) -> Optional[int]:
    parsed = _as_int(value)
    if parsed is None or parsed <= 0:
        return None
    return parsed


def _as_float(value: Any) -> Optional[float]:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None