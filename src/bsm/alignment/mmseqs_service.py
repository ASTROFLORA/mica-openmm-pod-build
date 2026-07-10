#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🧬 BSM MMSEQS2 SERVERLESS SERVICE
Service for executing sequence alignments using serverless MMseqs2 API.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
import os
from typing import Any, Dict, List, Optional
import httpx

from .blast_integration import BlastHit, BlastResult, _load_env_file_value

logger = logging.getLogger(__name__)


@dataclass
class MMseqsConfig:
    """Configuration for serverless MMseqs2 client"""
    endpoint_url: Optional[str] = None
    api_key: Optional[str] = None
    default_database: str = "uniprot_sprot"
    default_sensitivity: float = 7.5
    timeout_seconds: int = 30

    def __post_init__(self):
        # Load from environment variables or local .env file
        if not self.endpoint_url:
            self.endpoint_url = (
                os.getenv("MMSEQS_ENDPOINT_URL") or 
                os.getenv("MMSEQS_API_URL") or 
                _load_env_file_value("MMSEQS_ENDPOINT_URL") or 
                _load_env_file_value("MMSEQS_API_URL")
            )
        if not self.api_key:
            self.api_key = (
                os.getenv("MMSEQS_API_KEY") or 
                _load_env_file_value("MMSEQS_API_KEY")
            )
        
        env_database = os.getenv("MMSEQS_DATABASE") or _load_env_file_value("MMSEQS_DATABASE")
        if env_database:
            self.default_database = env_database
            
        env_sens = os.getenv("MMSEQS_SENSITIVITY") or _load_env_file_value("MMSEQS_SENSITIVITY")
        if env_sens:
            try:
                self.default_sensitivity = float(env_sens)
            except ValueError:
                pass
                
        env_timeout = os.getenv("MMSEQS_TIMEOUT") or _load_env_file_value("MMSEQS_TIMEOUT")
        if env_timeout:
            try:
                self.timeout_seconds = int(env_timeout)
            except ValueError:
                pass


class MMseqsService:
    """Service to handle async MMseqs2 sequence searches"""

    def __init__(self, config: Optional[MMseqsConfig] = None):
        self.config = config or MMseqsConfig()
        logger.info(f"🧬 MMseqsService initialized with endpoint: {self.config.endpoint_url}")

    async def search(
        self,
        sequence: str,
        query_id: str = "query",
        database: Optional[str] = None,
        sensitivity: Optional[float] = None,
        timeout_seconds: Optional[int] = None
    ) -> BlastResult:
        """
        Executes an async MMseqs2 search via HTTP POST.

        Args:
            sequence: FASTA sequence (amino acids)
            query_id: Query identifier
            database: Target database name
            sensitivity: Search sensitivity parameter
            timeout_seconds: HTTP client timeout

        Returns:
            BlastResult containing matching hits
        """
        start_time = time.time()
        errors: List[str] = []
        warnings: List[str] = []
        hits: List[BlastHit] = []

        endpoint = self.config.endpoint_url
        if not endpoint:
            errors.append("MMseqs2 endpoint URL not configured.")
            return self._build_empty_result(query_id, sequence, errors, warnings)

        # Normalize endpoint URL to API path
        url = endpoint
        if not url.endswith("/api/v1/mmseqs2/search") and not url.endswith("/api/v1/mmseqs2/search/"):
            # If endpoint is a base URL, append path
            url = url.rstrip("/") + "/api/v1/mmseqs2/search"

        db = database or self.config.default_database
        sens = sensitivity or self.config.default_sensitivity
        timeout = timeout_seconds or self.config.timeout_seconds

        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
            headers["X-API-Key"] = self.config.api_key

        payload = {
            "sequence": sequence,
            "database": db,
            "sensitivity": sens
        }

        try:
            logger.debug(f"Sending MMseqs2 request to {url} for query {query_id}")
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, json=payload, headers=headers)
                
                if response.status_code != 200:
                    errors.append(f"MMseqs2 service returned status {response.status_code}: {response.text}")
                    return self._build_empty_result(query_id, sequence, errors, warnings)

                data = response.json()
                raw_hits = data.get("hits") if isinstance(data, dict) else data
                if not isinstance(raw_hits, list):
                    errors.append(f"Unexpected MMseqs2 response format: {data}")
                    return self._build_empty_result(query_id, sequence, errors, warnings)

                for raw_hit in raw_hits:
                    try:
                        hit = BlastHit(
                            query_id=query_id,
                            subject_id=raw_hit.get("subject_id") or raw_hit.get("target_id") or raw_hit.get("sseqid") or raw_hit.get("accession") or "unknown",
                            identity=float(raw_hit.get("identity") or raw_hit.get("pident") or raw_hit.get("sequence_identity") or 0.0),
                            alignment_length=int(raw_hit.get("alignment_length") or raw_hit.get("align_len") or raw_hit.get("length") or 0),
                            mismatches=int(raw_hit.get("mismatches") or 0),
                            gap_opens=int(raw_hit.get("gap_opens") or raw_hit.get("gaps") or 0),
                            query_start=int(raw_hit.get("query_start") or raw_hit.get("qstart") or 1),
                            query_end=int(raw_hit.get("query_end") or raw_hit.get("qend") or len(sequence)),
                            subject_start=int(raw_hit.get("subject_start") or raw_hit.get("sstart") or 1),
                            subject_end=int(raw_hit.get("subject_end") or raw_hit.get("send") or 1),
                            e_value=float(raw_hit.get("e_value") or raw_hit.get("evalue") or raw_hit.get("eval") or 0.0),
                            bit_score=float(raw_hit.get("bit_score") or raw_hit.get("bitscore") or raw_hit.get("score") or 0.0),
                            subject_title=raw_hit.get("subject_title") or raw_hit.get("title") or raw_hit.get("description"),
                            subject_length=raw_hit.get("subject_length") or raw_hit.get("slen"),
                            query_coverage=raw_hit.get("query_coverage") or raw_hit.get("qcov")
                        )
                        hits.append(hit)
                    except Exception as e:
                        warnings.append(f"Failed to parse MMseqs2 hit details: {e}. Raw hit: {raw_hit}")

        except httpx.TimeoutException:
            errors.append(f"MMseqs2 request timed out after {timeout}s")
        except Exception as e:
            errors.append(f"MMseqs2 connection error: {str(e)}")

        execution_time_ms = (time.time() - start_time) * 1000

        return BlastResult(
            query_id=query_id,
            query_length=len(sequence),
            hits=hits,
            execution_time_ms=execution_time_ms,
            database_used=db,
            program_used="mmseqs2",
            parameters={"sensitivity": sens},
            errors=errors,
            warnings=warnings
        )

    def _build_empty_result(
        self,
        query_id: str,
        sequence: str,
        errors: List[str],
        warnings: List[str]
    ) -> BlastResult:
        return BlastResult(
            query_id=query_id,
            query_length=len(sequence),
            hits=[],
            execution_time_ms=0.0,
            database_used="unknown",
            program_used="mmseqs2",
            parameters={},
            errors=errors,
            warnings=warnings
        )
