"""UniProt Ground-Truth Stockpile (LMP v3 acquisition).

Stores per-accession snapshots as immutable evidence:
- entry.json.gz (raw UniProtKBEntry JSON)
- meta.json     (fetch metadata + entryAudit versions)

Design goals:
- Robust against transient network failures and rate limits.
- Deterministic/skippable for massive dataset builds.
- Optional refresh mode using `entryAudit` to invalidate cache.

This module intentionally uses `requests` (already used across the repo).
"""

from __future__ import annotations

import gzip
import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlparse

import requests


JsonDict = Dict[str, Any]


@dataclass(frozen=True)
class StockpileResult:
    accession: str
    dest_dir: Path
    entry_path: Path
    meta_path: Path
    status: str  # "downloaded" | "skipped" | "refreshed" | "error"
    http_status: Optional[int] = None
    reason: Optional[str] = None


class UniProtStockpiler:
    """Downloader for UniProtKB ground-truth snapshots."""

    def __init__(
        self,
        *,
        base_url: str = "https://rest.uniprot.org/uniprotkb",
        timeout_s: float = 30.0,
        max_retries: int = 5,
        base_sleep_s: float = 0.5,
        jitter_s: float = 0.25,
        request_min_interval_s: float = 0.2,
        request_min_interval_by_host: Optional[Dict[str, float]] = None,
        user_agent: str = "astroflora-bsm-uniprot-stockpiler/1.0",
        session: Optional[requests.Session] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = float(timeout_s)
        self.max_retries = int(max_retries)
        self.base_sleep_s = float(base_sleep_s)
        self.jitter_s = float(jitter_s)

        self.request_min_interval_s = max(0.0, float(request_min_interval_s))
        self.request_min_interval_by_host = request_min_interval_by_host or {}

        self.user_agent = user_agent
        self.session = session or requests.Session()
        self.sleep_fn = sleep_fn or time.sleep

        self._host_next_allowed_at: Dict[str, float] = {}
        self._host_lock = None  # lazy import threading only if needed

    def _get_host_lock(self):
        if self._host_lock is None:
            import threading

            self._host_lock = threading.Lock()
        return self._host_lock

    def _host_min_interval(self, url: str) -> float:
        try:
            host = urlparse(url).netloc
        except Exception:
            host = ""
        if host and host in self.request_min_interval_by_host:
            try:
                return max(0.0, float(self.request_min_interval_by_host[host]))
            except Exception:
                return self.request_min_interval_s
        return self.request_min_interval_s

    def _throttle_url(self, url: str) -> None:
        min_interval_s = self._host_min_interval(url)
        if min_interval_s <= 0:
            return

        try:
            host = urlparse(url).netloc
        except Exception:
            host = ""
        if not host:
            return

        now = time.time()
        wait_s = 0.0
        lock = self._get_host_lock()
        with lock:
            next_allowed = float(self._host_next_allowed_at.get(host, 0.0))
            if now < next_allowed:
                wait_s = next_allowed - now
                self._host_next_allowed_at[host] = next_allowed + min_interval_s
            else:
                self._host_next_allowed_at[host] = now + min_interval_s

        if wait_s > 0:
            self.sleep_fn(wait_s)

    def _request_with_backoff(self, method: str, url: str, *, timeout_s: float, **kwargs) -> requests.Response:
        last_exc: Optional[Exception] = None

        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("Accept", "application/json")
        headers.setdefault("User-Agent", self.user_agent)

        for attempt in range(self.max_retries):
            try:
                self._throttle_url(url)
                resp = self.session.request(method, url, headers=headers, timeout=timeout_s, **kwargs)

                if resp.status_code in (429, 503):
                    retry_after = resp.headers.get("Retry-After")
                    try:
                        retry_s = float(retry_after) if retry_after is not None else None
                    except Exception:
                        retry_s = None
                    wait_s = retry_s if retry_s is not None else (self.base_sleep_s * (2**attempt))
                    wait_s = wait_s + random.uniform(0, self.jitter_s)
                    self.sleep_fn(wait_s)
                    continue

                resp.raise_for_status()
                return resp
            except Exception as e:
                last_exc = e
                if attempt >= self.max_retries - 1:
                    break
                wait_s = (self.base_sleep_s * (2**attempt)) + random.uniform(0, self.jitter_s)
                self.sleep_fn(wait_s)

        raise last_exc if last_exc is not None else RuntimeError("request failed")

    def _atomic_write_text(self, path: Path, text: str) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)

    def _atomic_write_gzip_json(self, path: Path, payload: JsonDict) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        with gzip.open(tmp, "wb") as f:
            f.write(raw)
        tmp.replace(path)

    def _read_meta(self, meta_path: Path) -> Optional[JsonDict]:
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _extract_entry_audit(self, entry: JsonDict) -> JsonDict:
        audit = entry.get("entryAudit")
        if not isinstance(audit, dict):
            return {}
        keys = [
            "sequenceVersion",
            "entryVersion",
            "firstPublicDate",
            "lastAnnotationUpdateDate",
            "lastSequenceUpdateDate",
        ]
        return {k: audit.get(k) for k in keys if k in audit}

    def fetch_entry_audit(self, accession: str) -> Tuple[Optional[JsonDict], Optional[int]]:
        """Fetch a minimal payload to compare `entryAudit` versions.

        Uses `fields=entryAudit` to reduce payload.
        """
        url = f"{self.base_url}/{accession}"
        resp = self._request_with_backoff(
            "GET",
            url,
            timeout_s=self.timeout_s,
            params={"format": "json", "fields": "entryAudit"},
        )
        try:
            data = resp.json()
        except Exception:
            return None, resp.status_code
        if not isinstance(data, dict):
            return None, resp.status_code
        return self._extract_entry_audit(data), resp.status_code

    def stockpile_entry(
        self,
        accession: str,
        *,
        dataset_root: Path,
        fields: Optional[str] = None,
        refresh: bool = False,
        allow_skip_if_present: bool = True,
        force: bool = False,
    ) -> StockpileResult:
        """Download & persist a UniProtKBEntry snapshot.

        Args:
            accession: UniProt accession (e.g., P12931).
            dataset_root: Root folder that will contain per-accession subfolders.
            fields: Optional UniProt `fields` parameter. If None, fetch full JSON.
            refresh: If True, compares `entryAudit` versions and re-downloads when changed.
            allow_skip_if_present: If True and files exist, skips unless refresh/force.
            force: If True, always re-download.
        """
        accession = str(accession).strip()
        if not accession:
            raise ValueError("accession is required")

        dest_dir = Path(dataset_root) / accession
        dest_dir.mkdir(parents=True, exist_ok=True)

        entry_path = dest_dir / "entry.json.gz"
        meta_path = dest_dir / "meta.json"

        if not force and allow_skip_if_present and entry_path.exists() and meta_path.exists() and not refresh:
            return StockpileResult(
                accession=accession,
                dest_dir=dest_dir,
                entry_path=entry_path,
                meta_path=meta_path,
                status="skipped",
                reason="present",
            )

        previous_meta = self._read_meta(meta_path) if meta_path.exists() else None
        previous_audit = None
        if isinstance(previous_meta, dict):
            previous_audit = previous_meta.get("entryAudit")

        if refresh and not force and entry_path.exists() and meta_path.exists() and isinstance(previous_audit, dict):
            try:
                current_audit, _ = self.fetch_entry_audit(accession)
                if isinstance(current_audit, dict) and current_audit and current_audit == previous_audit:
                    return StockpileResult(
                        accession=accession,
                        dest_dir=dest_dir,
                        entry_path=entry_path,
                        meta_path=meta_path,
                        status="skipped",
                        reason="entryAudit_unchanged",
                    )
            except Exception:
                # On refresh-check failure, proceed to full download.
                pass

        url = f"{self.base_url}/{accession}"
        params: Dict[str, str] = {"format": "json"}
        if fields:
            params["fields"] = fields

        started_at = time.time()
        try:
            resp = self._request_with_backoff("GET", url, timeout_s=self.timeout_s, params=params)
            http_status = resp.status_code
            data = resp.json()
            if not isinstance(data, dict):
                raise ValueError("UniProt response is not an object")

            fetched_audit = self._extract_entry_audit(data)

            meta: JsonDict = {
                "accession": accession,
                "url": url,
                "params": params,
                "fetchedAtEpochMs": int(time.time() * 1000),
                "latencyMs": int((time.time() - started_at) * 1000),
                "httpStatus": http_status,
                "contentType": resp.headers.get("Content-Type"),
                "etag": resp.headers.get("ETag"),
                "lastModified": resp.headers.get("Last-Modified"),
                "entryAudit": fetched_audit,
            }

            # Persist atomically.
            self._atomic_write_gzip_json(entry_path, data)
            self._atomic_write_text(meta_path, json.dumps(meta, ensure_ascii=False, indent=2))

            status = "downloaded"
            if refresh and entry_path.exists() and meta_path.exists() and previous_audit is not None:
                status = "refreshed"

            return StockpileResult(
                accession=accession,
                dest_dir=dest_dir,
                entry_path=entry_path,
                meta_path=meta_path,
                status=status,
                http_status=http_status,
            )
        except Exception as e:
            err_meta: JsonDict = {
                "accession": accession,
                "url": url,
                "params": params,
                "fetchedAtEpochMs": int(time.time() * 1000),
                "latencyMs": int((time.time() - started_at) * 1000),
                "error": str(e),
            }
            try:
                self._atomic_write_text(meta_path, json.dumps(err_meta, ensure_ascii=False, indent=2))
            except Exception:
                pass
            return StockpileResult(
                accession=accession,
                dest_dir=dest_dir,
                entry_path=entry_path,
                meta_path=meta_path,
                status="error",
                reason=str(e),
            )
