#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🧬 BSM BLAST INTEGRATION
Integración con BLAST para alineamiento de secuencias de proteínas.

Soporta:
- BLASTP: Protein vs Protein database
- BLASTX: Translated nucleotide vs Protein database
- Integración con NCBI BLAST+ local o remoto
- Caching de resultados para queries frecuentes

Author: BSM Team (Modernization based on DEEPRESEARCH + Hybrid RAG Architecture)
Date: 2025
Version: 2.0.0
"""

import asyncio
import hashlib
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)


def _load_env_file_value(name: str) -> Optional[str]:
    """Load one environment value from a nearby .env file without logging secrets."""
    candidate_roots = [Path.cwd(), *Path(__file__).resolve().parents]
    seen = set()
    for root in candidate_roots:
        env_path = root / ".env"
        if env_path in seen or not env_path.exists():
            continue
        seen.add(env_path)
        try:
            for raw_line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key.strip() == name:
                    return value.strip().strip('"').strip("'")
        except OSError:
            continue
    return None


# ============================================================================
# CONFIGURATION
# ============================================================================

class BlastProgram(Enum):
    """Programas BLAST disponibles"""
    BLASTP = "blastp"   # Protein query vs protein database
    BLASTN = "blastn"   # Nucleotide query vs nucleotide database
    BLASTX = "blastx"   # Translated nucleotide query vs protein database
    TBLASTN = "tblastn" # Protein query vs translated nucleotide database


class BlastDatabase(Enum):
    """Bases de datos BLAST comunes"""
    SWISSPROT = "swissprot"
    PDB = "pdb"
    NR = "nr"                  # Non-redundant protein sequences
    REFSEQ_PROTEIN = "refseq_protein"
    UNIPROT_SPROT = "uniprot_sprot"
    CUSTOM = "custom"


@dataclass
class BlastConfig:
    """Configuración para ejecución de BLAST"""
    
    # Programa por defecto
    program: BlastProgram = BlastProgram.BLASTP
    
    # Base de datos
    database: BlastDatabase = BlastDatabase.SWISSPROT
    custom_database_path: Optional[str] = None
    
    # Parámetros de búsqueda
    evalue_threshold: float = 1e-5
    max_target_seqs: int = 50
    word_size: int = 3
    
    # Parámetros de scoring
    matrix: str = "BLOSUM62"
    gap_open: int = 11
    gap_extend: int = 1
    
    # Opciones de output
    outfmt: int = 6  # Tabular format (más fácil de parsear)
    
    # Execution
    num_threads: int = 4
    timeout_seconds: int = 300
    
    # BLAST+ executable paths
    blast_path: str = ""  # Empty = use system PATH
    
    # Remote BLAST (NCBI)
    use_remote: bool = False
    ncbi_api_key: Optional[str] = None
    
    # Cache
    cache_enabled: bool = True
    cache_dir: str = "./cache/blast"
    cache_ttl_hours: int = 24
    
    # MMseqs2 Serverless overrides
    mmseqs_endpoint_url: Optional[str] = None
    mmseqs_api_key: Optional[str] = None
    mmseqs_database: Optional[str] = None
    mmseqs_sensitivity: Optional[float] = None
    
    def __post_init__(self):
        # Try to find BLAST+ installation
        if not self.blast_path:
            self.blast_path = self._find_blast_path()
        
        # Load NCBI API key from environment
        if not self.ncbi_api_key:
            self.ncbi_api_key = os.getenv("NCBI_API_KEY") or _load_env_file_value("NCBI_API_KEY")

        # Load MMseqs2 parameters from environment
        if not self.mmseqs_endpoint_url:
            self.mmseqs_endpoint_url = (
                os.getenv("MMSEQS_ENDPOINT_URL") or 
                os.getenv("MMSEQS_API_URL") or 
                _load_env_file_value("MMSEQS_ENDPOINT_URL") or 
                _load_env_file_value("MMSEQS_API_URL")
            )
        if not self.mmseqs_api_key:
            self.mmseqs_api_key = os.getenv("MMSEQS_API_KEY") or _load_env_file_value("MMSEQS_API_KEY")
        if not self.mmseqs_database:
            self.mmseqs_database = os.getenv("MMSEQS_DATABASE") or _load_env_file_value("MMSEQS_DATABASE")
        if not self.mmseqs_sensitivity:
            sens_env = os.getenv("MMSEQS_SENSITIVITY") or _load_env_file_value("MMSEQS_SENSITIVITY")
            if sens_env:
                try:
                    self.mmseqs_sensitivity = float(sens_env)
                except ValueError:
                    pass
    
    def _find_blast_path(self) -> str:
        """Intenta encontrar instalación de BLAST+"""
        # Common paths
        common_paths = [
            "/usr/bin",
            "/usr/local/bin",
            "/opt/ncbi-blast+/bin",
            "C:\\Program Files\\NCBI\\blast-2.15.0+\\bin",
            "C:\\blast\\bin",
        ]
        
        for path in common_paths:
            blastp = Path(path) / ("blastp.exe" if os.name == "nt" else "blastp")
            if blastp.exists():
                return path
        
        return ""


@dataclass
class BlastHit:
    """Resultado individual de BLAST"""
    query_id: str
    subject_id: str
    identity: float           # Percentage identity
    alignment_length: int
    mismatches: int
    gap_opens: int
    query_start: int
    query_end: int
    subject_start: int
    subject_end: int
    e_value: float
    bit_score: float
    
    # Metadatos adicionales
    subject_title: Optional[str] = None
    subject_length: Optional[int] = None
    query_coverage: Optional[float] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convierte a diccionario"""
        return {
            "query_id": self.query_id,
            "subject_id": self.subject_id,
            "identity": self.identity,
            "alignment_length": self.alignment_length,
            "mismatches": self.mismatches,
            "gap_opens": self.gap_opens,
            "query_start": self.query_start,
            "query_end": self.query_end,
            "subject_start": self.subject_start,
            "subject_end": self.subject_end,
            "e_value": self.e_value,
            "bit_score": self.bit_score,
            "subject_title": self.subject_title,
            "subject_length": self.subject_length,
            "query_coverage": self.query_coverage,
        }


@dataclass
class BlastResult:
    """Resultado completo de búsqueda BLAST"""
    query_id: str
    query_length: int
    hits: List[BlastHit]
    execution_time_ms: float
    database_used: str
    program_used: str
    parameters: Dict[str, Any]
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    
    @property
    def num_hits(self) -> int:
        return len(self.hits)
    
    @property
    def best_hit(self) -> Optional[BlastHit]:
        """Retorna el mejor hit (menor e-value)"""
        if not self.hits:
            return None
        return min(self.hits, key=lambda h: h.e_value)
    
    def filter_by_evalue(self, max_evalue: float) -> List[BlastHit]:
        """Filtra hits por e-value máximo"""
        return [h for h in self.hits if h.e_value <= max_evalue]
    
    def filter_by_identity(self, min_identity: float) -> List[BlastHit]:
        """Filtra hits por identidad mínima"""
        return [h for h in self.hits if h.identity >= min_identity]
    
    def to_dict(self) -> Dict[str, Any]:
        """Convierte a diccionario"""
        return {
            "query_id": self.query_id,
            "query_length": self.query_length,
            "num_hits": self.num_hits,
            "hits": [h.to_dict() for h in self.hits],
            "execution_time_ms": self.execution_time_ms,
            "database_used": self.database_used,
            "program_used": self.program_used,
            "parameters": self.parameters,
            "errors": self.errors,
            "warnings": self.warnings,
        }


# ============================================================================
# BLAST SERVICE
# ============================================================================

class BlastService:
    """
    Servicio para ejecutar búsquedas BLAST.
    
    Soporta:
    - BLAST+ local (instalación requerida)
    - NCBI BLAST remoto (requiere API key para alto volumen)
    - Caching de resultados
    """
    
    def __init__(self, config: Optional[BlastConfig] = None):
        self.config = config or BlastConfig()
        self._cache: Dict[str, BlastResult] = {}
        
        # Crear directorio de cache
        if self.config.cache_enabled:
            Path(self.config.cache_dir).mkdir(parents=True, exist_ok=True)
        
        logger.info(f"🧬 BlastService initialized (remote={self.config.use_remote})")
    
    def _get_cache_key(self, sequence: str, program: BlastProgram, database: str) -> str:
        """Genera clave de cache determinística"""
        content = f"{program.value}:{database}:{sequence}"
        return hashlib.sha256(content.encode()).hexdigest()[:32]
    
    def _check_cache(self, cache_key: str) -> Optional[BlastResult]:
        """Busca resultado en cache"""
        if cache_key in self._cache:
            return self._cache[cache_key]
        
        # Check disk cache
        cache_file = Path(self.config.cache_dir) / f"{cache_key}.json"
        if cache_file.exists():
            import json
            try:
                with open(cache_file, 'r') as f:
                    data = json.load(f)
                    # Reconstruct BlastResult from dict
                    hits = [BlastHit(**h) for h in data.get("hits", [])]
                    result = BlastResult(
                        query_id=data["query_id"],
                        query_length=data["query_length"],
                        hits=hits,
                        execution_time_ms=data["execution_time_ms"],
                        database_used=data["database_used"],
                        program_used=data["program_used"],
                        parameters=data["parameters"],
                        errors=data.get("errors", []),
                        warnings=data.get("warnings", [])
                    )
                    self._cache[cache_key] = result
                    return result
            except Exception as e:
                logger.warning(f"Failed to load cached BLAST result: {e}")
        
        return None
    
    def _save_to_cache(self, cache_key: str, result: BlastResult) -> None:
        """Guarda resultado en cache"""
        self._cache[cache_key] = result
        
        if self.config.cache_enabled:
            import json
            cache_file = Path(self.config.cache_dir) / f"{cache_key}.json"
            try:
                with open(cache_file, 'w') as f:
                    json.dump(result.to_dict(), f, indent=2)
            except Exception as e:
                logger.warning(f"Failed to cache BLAST result: {e}")
    
    async def search(
        self,
        sequence: str,
        query_id: str = "query",
        program: Optional[BlastProgram] = None,
        database: Optional[Union[str, BlastDatabase]] = None,
        evalue: Optional[float] = None,
        max_hits: Optional[int] = None
    ) -> BlastResult:
        """
        Ejecuta búsqueda BLAST.
        
        Args:
            sequence: Secuencia de proteína (aminoácidos)
            query_id: ID para la query
            program: Programa BLAST (default: BLASTP)
            database: Base de datos (default: desde config)
            evalue: Threshold e-value (default: desde config)
            max_hits: Máximo de hits (default: desde config)
            
        Returns:
            BlastResult con todos los hits
        """
        import time
        start_time = time.time()
        
        # Usar defaults de config
        program = program or self.config.program
        database = database or (
            self.config.custom_database_path 
            if self.config.database == BlastDatabase.CUSTOM 
            else self.config.database.value
        )
        if isinstance(database, BlastDatabase):
            database = database.value
        evalue = evalue or self.config.evalue_threshold
        max_hits = max_hits or self.config.max_target_seqs
        
        # Check cache
        cache_key = self._get_cache_key(sequence, program, database)
        cached = self._check_cache(cache_key)
        if cached:
            logger.debug(f"🎯 BLAST cache hit for {query_id}")
            return cached
        
        # Execute BLAST or MMseqs2 Serverless
        if self.config.mmseqs_endpoint_url:
            from .mmseqs_service import MMseqsService, MMseqsConfig
            mmseqs_cfg = MMseqsConfig(
                endpoint_url=self.config.mmseqs_endpoint_url,
                api_key=self.config.mmseqs_api_key,
                default_database=database or self.config.mmseqs_database or "uniprot_sprot",
                default_sensitivity=self.config.mmseqs_sensitivity or 7.5,
                timeout_seconds=self.config.timeout_seconds
            )
            mmseqs_svc = MMseqsService(mmseqs_cfg)
            result = await mmseqs_svc.search(
                sequence=sequence,
                query_id=query_id,
                database=database or self.config.mmseqs_database,
                sensitivity=self.config.mmseqs_sensitivity,
                timeout_seconds=self.config.timeout_seconds
            )
        elif self.config.use_remote:
            result = await self._run_remote_blast(
                sequence, query_id, program, database, evalue, max_hits
            )
        else:
            result = await self._run_local_blast(
                sequence, query_id, program, database, evalue, max_hits
            )
        
        result.execution_time_ms = (time.time() - start_time) * 1000
        
        # Save to cache
        self._save_to_cache(cache_key, result)
        
        logger.info(
            f"🧬 BLAST search complete: {result.num_hits} hits in "
            f"{result.execution_time_ms:.1f}ms"
        )
        
        return result
    
    async def _run_local_blast(
        self,
        sequence: str,
        query_id: str,
        program: BlastProgram,
        database: str,
        evalue: float,
        max_hits: int
    ) -> BlastResult:
        """Ejecuta BLAST+ local"""
        
        errors: List[str] = []
        warnings: List[str] = []
        hits: List[BlastHit] = []
        
        # Build executable path
        if self.config.blast_path:
            executable = Path(self.config.blast_path) / program.value
        else:
            executable = program.value
        
        # Create temp files for query and output
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.fasta', delete=False
        ) as query_file:
            query_file.write(f">{query_id}\n{sequence}\n")
            query_path = query_file.name
        
        # mktemp() is deprecated (TOCTOU race) — use mkstemp() instead
        output_fd, output_path = tempfile.mkstemp(suffix='.txt')
        os.close(output_fd)
        
        try:
            # Build command
            cmd = [
                str(executable),
                "-query", query_path,
                "-db", database,
                "-evalue", str(evalue),
                "-max_target_seqs", str(max_hits),
                "-num_threads", str(self.config.num_threads),
                "-outfmt", "6 qseqid sseqid pident length mismatch gapopen qstart qend sstart send evalue bitscore stitle",
                "-out", output_path,
            ]
            
            # Add matrix and gap penalties
            cmd.extend(["-matrix", self.config.matrix])
            cmd.extend(["-gapopen", str(self.config.gap_open)])
            cmd.extend(["-gapextend", str(self.config.gap_extend)])
            
            # Execute
            logger.debug(f"Running BLAST: {' '.join(cmd)}")
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self.config.timeout_seconds
            )
            
            if process.returncode != 0:
                errors.append(f"BLAST failed with code {process.returncode}: {stderr.decode()}")
            
            # Parse output
            if Path(output_path).exists():
                hits = self._parse_tabular_output(output_path, query_id)
            
        except asyncio.TimeoutError:
            errors.append(f"BLAST timed out after {self.config.timeout_seconds}s")
        except FileNotFoundError:
            errors.append(f"BLAST executable not found: {executable}")
        except Exception as e:
            errors.append(f"BLAST error: {str(e)}")
        finally:
            # Cleanup temp files
            try:
                os.unlink(query_path)
                if Path(output_path).exists():
                    os.unlink(output_path)
            except Exception:
                pass
        
        return BlastResult(
            query_id=query_id,
            query_length=len(sequence),
            hits=hits,
            execution_time_ms=0.0,  # Filled by caller
            database_used=database,
            program_used=program.value,
            parameters={
                "evalue": evalue,
                "max_hits": max_hits,
                "matrix": self.config.matrix,
            },
            errors=errors,
            warnings=warnings
        )
    
    async def _run_remote_blast(
        self,
        sequence: str,
        query_id: str,
        program: BlastProgram,
        database: str,
        evalue: float,
        max_hits: int
    ) -> BlastResult:
        """Ejecuta BLAST remoto via NCBI"""
        
        try:
            from Bio.Blast import NCBIWWW, NCBIXML
        except ImportError:
            return BlastResult(
                query_id=query_id,
                query_length=len(sequence),
                hits=[],
                execution_time_ms=0.0,
                database_used=database,
                program_used=program.value,
                parameters={},
                errors=["Biopython not installed. Install with: pip install biopython"]
            )
        
        errors: List[str] = []
        warnings: List[str] = []
        hits: List[BlastHit] = []
        
        try:
            # Run remote BLAST
            url_base = "https://blast.ncbi.nlm.nih.gov/Blast.cgi"
            if self.config.ncbi_api_key:
                url_base = f"{url_base}?API_KEY={self.config.ncbi_api_key}"

            result_handle = NCBIWWW.qblast(
                program.value,
                database,
                sequence,
                url_base=url_base,
                expect=evalue,
                hitlist_size=max_hits,
                format_type="XML"
            )
            
            # Parse XML results
            blast_records = NCBIXML.parse(result_handle)
            
            for record in blast_records:
                for alignment in record.alignments:
                    for hsp in alignment.hsps:
                        hit = BlastHit(
                            query_id=query_id,
                            subject_id=alignment.hit_id,
                            identity=hsp.identities / hsp.align_length * 100,
                            alignment_length=hsp.align_length,
                            mismatches=hsp.align_length - hsp.identities,
                            gap_opens=hsp.gaps,
                            query_start=hsp.query_start,
                            query_end=hsp.query_end,
                            subject_start=hsp.sbjct_start,
                            subject_end=hsp.sbjct_end,
                            e_value=hsp.expect,
                            bit_score=hsp.bits,
                            subject_title=alignment.hit_def,
                            subject_length=alignment.length
                        )
                        hits.append(hit)
            
            result_handle.close()
            
        except Exception as e:
            errors.append(f"Remote BLAST error: {str(e)}")
        
        return BlastResult(
            query_id=query_id,
            query_length=len(sequence),
            hits=hits,
            execution_time_ms=0.0,
            database_used=database,
            program_used=program.value,
            parameters={
                "evalue": evalue,
                "max_hits": max_hits,
                "remote": True
            },
            errors=errors,
            warnings=warnings
        )
    
    def _parse_tabular_output(self, output_path: str, query_id: str) -> List[BlastHit]:
        """Parsea output tabular de BLAST (-outfmt 6)"""
        hits = []
        
        with open(output_path, 'r') as f:
            for line in f:
                if line.strip():
                    fields = line.strip().split('\t')
                    if len(fields) >= 12:
                        try:
                            hit = BlastHit(
                                query_id=fields[0],
                                subject_id=fields[1],
                                identity=float(fields[2]),
                                alignment_length=int(fields[3]),
                                mismatches=int(fields[4]),
                                gap_opens=int(fields[5]),
                                query_start=int(fields[6]),
                                query_end=int(fields[7]),
                                subject_start=int(fields[8]),
                                subject_end=int(fields[9]),
                                e_value=float(fields[10]),
                                bit_score=float(fields[11]),
                                subject_title=fields[12] if len(fields) > 12 else None
                            )
                            hits.append(hit)
                        except (ValueError, IndexError) as e:
                            logger.warning(f"Failed to parse BLAST line: {e}")
        
        return hits
    
    async def batch_search(
        self,
        sequences: List[Tuple[str, str]],  # [(query_id, sequence), ...]
        program: Optional[BlastProgram] = None,
        database: Optional[str] = None
    ) -> List[BlastResult]:
        """
        Ejecuta búsquedas BLAST en lote.
        
        Args:
            sequences: Lista de (query_id, sequence) tuples
            program: Programa BLAST
            database: Base de datos
            
        Returns:
            Lista de BlastResult
        """
        results = []
        
        for query_id, sequence in sequences:
            result = await self.search(
                sequence=sequence,
                query_id=query_id,
                program=program,
                database=database
            )
            results.append(result)
        
        return results
    
    def get_top_hits_for_rrf(
        self,
        blast_result: BlastResult,
        max_hits: int = 20
    ) -> List[Dict[str, Any]]:
        """
        Formatea hits de BLAST para fusión RRF.
        
        Args:
            blast_result: Resultado de BLAST
            max_hits: Máximo de hits a retornar
            
        Returns:
            Lista de dicts con subject_id, bit_score, y metadatos
        """
        # Ordenar por bit_score descendente (ya deberían estar ordenados)
        sorted_hits = sorted(
            blast_result.hits[:max_hits],
            key=lambda h: h.bit_score,
            reverse=True
        )
        
        rrf_compatible = []
        for hit in sorted_hits:
            rrf_compatible.append({
                "subject_id": hit.subject_id,
                "bit_score": hit.bit_score,
                "e_value": hit.e_value,
                "identity": hit.identity,
                "alignment_length": hit.alignment_length,
                "subject_title": hit.subject_title
            })
        
        return rrf_compatible


# ============================================================================
# MOCK BLAST SERVICE (for testing without BLAST+ installation)
# ============================================================================

class MockBlastService:
    """
    Mock BLAST service para testing y desarrollo sin instalación de BLAST+.
    
    Genera resultados sintéticos basados en similitud de secuencias simple.
    """
    
    def __init__(self, mock_database: Optional[Dict[str, str]] = None):
        """
        Args:
            mock_database: Dict {protein_id: sequence} para búsquedas mock
        """
        self.mock_database = mock_database or {}
        logger.info("🧬 MockBlastService initialized for testing")
    
    def add_to_database(self, protein_id: str, sequence: str):
        """Añade secuencia a base de datos mock"""
        self.mock_database[protein_id] = sequence.upper()
    
    def _simple_identity(self, seq1: str, seq2: str) -> float:
        """Calcula identidad simple (no es un alineamiento real)"""
        # Simple character matching (not real alignment)
        min_len = min(len(seq1), len(seq2))
        if min_len == 0:
            return 0.0
        
        matches = sum(1 for a, b in zip(seq1[:min_len], seq2[:min_len]) if a == b)
        return matches / min_len * 100
    
    async def search(
        self,
        sequence: str,
        query_id: str = "query",
        **kwargs
    ) -> BlastResult:
        """Búsqueda mock basada en identidad simple"""
        import time
        import random
        start_time = time.time()
        
        sequence = sequence.upper()
        hits = []
        
        for subject_id, subject_seq in self.mock_database.items():
            identity = self._simple_identity(sequence, subject_seq)
            
            if identity > 20:  # Threshold mínimo
                hit = BlastHit(
                    query_id=query_id,
                    subject_id=subject_id,
                    identity=identity,
                    alignment_length=min(len(sequence), len(subject_seq)),
                    mismatches=int((100 - identity) / 100 * len(sequence)),
                    gap_opens=random.randint(0, 5),
                    query_start=1,
                    query_end=len(sequence),
                    subject_start=1,
                    subject_end=len(subject_seq),
                    e_value=10 ** (-identity / 10),  # Fake e-value
                    bit_score=identity * 2,  # Fake bit score
                    subject_title=f"Mock protein {subject_id}"
                )
                hits.append(hit)
        
        # Sort by e-value
        hits.sort(key=lambda h: h.e_value)
        
        return BlastResult(
            query_id=query_id,
            query_length=len(sequence),
            hits=hits[:50],  # Max 50 hits
            execution_time_ms=(time.time() - start_time) * 1000,
            database_used="mock_database",
            program_used="mock_blastp",
            parameters={"mock": True},
            warnings=["Using MockBlastService - results are synthetic"]
        )


# ============================================================================
# FACTORY FUNCTIONS
# ============================================================================

def create_blast_service(
    use_remote: bool = False,
    database: BlastDatabase = BlastDatabase.SWISSPROT,
    custom_db_path: Optional[str] = None
) -> BlastService:
    """
    Factory para crear servicio BLAST configurado.
    
    Args:
        use_remote: Si usar NCBI BLAST remoto
        database: Base de datos a usar
        custom_db_path: Path a base de datos custom (si database=CUSTOM)
        
    Returns:
        BlastService configurado
    """
    config = BlastConfig(
        use_remote=use_remote,
        database=database,
        custom_database_path=custom_db_path
    )
    return BlastService(config)


def create_mock_blast_service(
    initial_sequences: Optional[Dict[str, str]] = None
) -> MockBlastService:
    """Factory para crear servicio BLAST mock"""
    return MockBlastService(initial_sequences)


# ============================================================================
# EXPORTS
# ============================================================================

__all__ = [
    "BlastProgram",
    "BlastDatabase",
    "BlastConfig",
    "BlastHit",
    "BlastResult",
    "BlastService",
    "MockBlastService",
    "create_blast_service",
    "create_mock_blast_service",
]
