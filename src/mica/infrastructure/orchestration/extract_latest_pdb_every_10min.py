#!/usr/bin/env python3
import os
import time
from pathlib import Path

SNAPSHOTS_PDB = Path(os.environ.get("SNAPSHOTS_PDB", "/workspace/wnk4_run/runs/replica_1/replica_1_snapshots.pdb"))
LATEST_PDB = Path(os.environ.get("LATEST_PDB", "/workspace/wnk4_run/runs/replica_1/replica_1_latest.pdb"))
INTERVAL_SEC = int(os.environ.get("INTERVAL_SEC", "600"))


def extract_last_model(src: Path, dst: Path) -> bool:
    if not src.exists() or src.stat().st_size == 0:
        return False

    lines = src.read_text(encoding="utf-8", errors="ignore").splitlines()
    model_starts = [index for index, line in enumerate(lines) if line.startswith("MODEL")]
    model_ends = [index for index, line in enumerate(lines) if line.startswith("ENDMDL")]

    if model_starts and model_ends and len(model_ends) >= len(model_starts):
        start = model_starts[-1]
        end = model_ends[-1]
        chunk = lines[start : end + 1]
    else:
        chunk = [line for line in lines if line.startswith(("ATOM", "HETATM", "TER", "END"))]

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("\n".join(chunk) + "\n", encoding="utf-8")
    return True


def main() -> None:
    print(f"Watching snapshots: {SNAPSHOTS_PDB}")
    print(f"Writing latest PDB: {LATEST_PDB}")
    print(f"Interval: {INTERVAL_SEC}s")
    while True:
        try:
            ok = extract_last_model(SNAPSHOTS_PDB, LATEST_PDB)
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            if ok:
                print(f"[{timestamp}] Updated latest PDB")
            else:
                print(f"[{timestamp}] Snapshot file not ready")
        except Exception as exc:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{timestamp}] Error: {exc}")
        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    main()