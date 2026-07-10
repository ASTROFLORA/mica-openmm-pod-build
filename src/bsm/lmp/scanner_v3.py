"""LMP v3 Scanner (Orchestrator).

Batch pipeline for building LMP v3 datasets from UniProt ground-truth snapshots.

Key behaviors:
- Checkpointing: skip accessions that already have an output XML.
- Manifest: write JSONL records per accession.
- Optional acquisition: can fetch missing snapshots via UniProtStockpiler.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import concurrent.futures

try:
    from tqdm import tqdm
except Exception:  # pragma: no cover
    def tqdm(iterable, **kwargs):
        return iterable


from src.bsm.utils.uniprot_stockpile import UniProtStockpiler

from .generator_v3 import LmpV3Generator


logger = logging.getLogger("LMPScannerV3")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")


class LMPScannerV3:
    def __init__(
        self,
        *,
        output_dir: str | Path = "./output",
        stockpile_root: str | Path = "./lmp_v3_stockpile",
        validate_xsd: bool = True,
        stockpiler: Optional[UniProtStockpiler] = None,
        generator: Optional[LmpV3Generator] = None,
    ):
        self.output_dir = str(output_dir)
        self.stockpile_root = Path(stockpile_root)
        self.stockpiler = stockpiler or UniProtStockpiler()
        self.generator = generator or LmpV3Generator(validate=validate_xsd)

    def _safe_mkdir(self, path: str) -> None:
        os.makedirs(path, exist_ok=True)

    def _setup_output(self, subdir: str) -> tuple[str, str]:
        target_dir = os.path.join(self.output_dir, subdir)
        self._safe_mkdir(target_dir)
        manifest_path = os.path.join(target_dir, "dataset_manifest.jsonl")
        return target_dir, manifest_path

    def _output_path_for(self, accession: str, target_dir: str) -> Path:
        return Path(target_dir) / f"{accession}.xml"

    def _is_processed(self, accession: str, target_dir: str) -> bool:
        return self._output_path_for(accession, target_dir).exists()

    def _save_to_manifest(self, manifest_path: str, record: Dict[str, Any]) -> None:
        with open(manifest_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _atomic_write_text(self, path: Path, text: str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8", dir=str(path.parent), prefix=path.name, suffix=".tmp") as tf:
            tf.write(text)
            tmp_name = tf.name
        os.replace(tmp_name, path)

    def _worker(
        self,
        accession: str,
        *,
        target_dir: str,
        fetch_missing: bool,
        refresh: bool,
        force: bool,
    ) -> Dict[str, Any]:
        accession = str(accession).strip()
        out_path = self._output_path_for(accession, target_dir)

        if out_path.exists():
            return {"accession": accession, "status": "skipped", "reason": "output_present", "output": str(out_path)}

        stockpile_res = None
        if fetch_missing:
            stockpile_res = self.stockpiler.stockpile_entry(
                accession,
                dataset_root=self.stockpile_root,
                refresh=bool(refresh),
                force=bool(force),
            )
            if getattr(stockpile_res, "status", None) == "error":
                return {
                    "accession": accession,
                    "status": "error",
                    "stage": "stockpile",
                    "reason": getattr(stockpile_res, "reason", None),
                    "stockpile": asdict(stockpile_res),
                }

        snapshot_dir = self.stockpile_root / accession
        entry_path = snapshot_dir / "entry.json.gz"
        if not entry_path.exists():
            return {
                "accession": accession,
                "status": "error",
                "stage": "load_snapshot",
                "reason": f"missing_snapshot: {entry_path}",
                "stockpile": asdict(stockpile_res) if stockpile_res is not None else None,
            }

        try:
            snap = self.generator.load_snapshot_dir(snapshot_dir, accession=accession)
            xml = self.generator.generate_xml(snap)
            self._atomic_write_text(out_path, xml)
            return {
                "accession": accession,
                "status": "generated",
                "output": str(out_path),
                "stockpile": asdict(stockpile_res) if stockpile_res is not None else None,
            }
        except Exception as e:
            return {
                "accession": accession,
                "status": "error",
                "stage": "generate_xml",
                "reason": str(e),
                "output": str(out_path),
                "stockpile": asdict(stockpile_res) if stockpile_res is not None else None,
            }

    def build_dataset(
        self,
        accessions: List[str],
        dataset_name: str,
        *,
        max_workers: int = 5,
        dry_run: bool = False,
        fetch_missing: bool = True,
        refresh: bool = False,
        force: bool = False,
    ) -> List[Dict[str, Any]]:
        if dry_run:
            logger.info("[DRY RUN] Would process %s accessions", len(accessions or []))
            logger.info("[DRY RUN] Output: %s", os.path.join(self.output_dir, dataset_name))
            logger.info("[DRY RUN] Stockpile root: %s", str(self.stockpile_root))
            return []

        if not accessions:
            logger.info("Nothing to process: accessions empty")
            return []

        target_dir, manifest_path = self._setup_output(dataset_name)
        pending = [acc for acc in accessions if not self._is_processed(str(acc).strip(), target_dir)]
        skipped_count = len(accessions) - len(pending)
        logger.info("Processing %s accessions (%s already existed)", len(pending), skipped_count)
        if not pending:
            return []

        results: List[Dict[str, Any]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=int(max_workers)) as executor:
            future_to_acc = {
                executor.submit(
                    self._worker,
                    acc,
                    target_dir=target_dir,
                    fetch_missing=bool(fetch_missing),
                    refresh=bool(refresh),
                    force=bool(force),
                ): acc
                for acc in pending
            }
            with tqdm(total=len(pending), desc=f"LMP v3 {dataset_name}") as pbar:
                for future in concurrent.futures.as_completed(future_to_acc):
                    res = future.result()
                    res.setdefault("dataset", dataset_name)
                    self._save_to_manifest(manifest_path, res)
                    results.append(res)
                    pbar.update(1)

        logger.info("Dataset '%s' completed. Manifest: %s", dataset_name, manifest_path)
        return results
