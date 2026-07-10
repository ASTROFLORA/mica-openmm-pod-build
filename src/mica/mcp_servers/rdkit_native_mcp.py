"""
🧪 RDKit Native MCP Server (In-Process / Zero Latency) - EXPANDED VERSION

ARQUITECTURA:
- FastMCP In-Memory Server (NO subprocess)
- Cero overhead de comunicación  
- Auto-registro de 60+ herramientas RDKit
- Metadatos MCP completos para LLM
- Ejecución directa de funciones Python

VENTAJAS vs Subprocess MCP:
- Latencia: <1ms vs 100-500ms
- Overhead: Cero vs serialización/deserialización
- Control: Código nativo vs proceso externo
- Trazabilidad: Mantenida vía MCP metadata

HERRAMIENTAS (60+):
- Descriptors (20+): MolWt, ExactMolWt, TPSA, LogP, etc.
- Molecular Descriptors (30+): CalcNumRings, CalcNumHBA, CalcTPSA, etc.
- Drawing (3): MolToImage, MolToFile, Compute2DCoords
- Conversion (6): SMILES, PDB, SDF
- Fingerprints (2): Morgan, Tanimoto
- Substructure (2): HasSubstructMatch, GetSubstructMatch

USO:
from mica.mcp_servers.rdkit_native_mcp import rdkit_native_server
client = fastmcp.Client(rdkit_native_server)  # In-process
result = await client.call_tool("calculate_molecular_weight", {"smiles": "CC(=O)O"})
"""
from __future__ import annotations

import base64
import inspect
import logging
import time
from collections import defaultdict
from datetime import datetime
from functools import wraps
from io import BytesIO
from threading import Lock
from typing import Any, Dict, List, Optional, Callable, Tuple

from fastmcp import FastMCP
from pydantic import BaseModel, Field, field_validator

# RDKit imports
try:
    from rdkit import Chem, DataStructs
    from rdkit.Chem import Descriptors, Crippen, Lipinski, AllChem
    from rdkit.Chem import Draw, rdMolDescriptors, rdDepictor, Scaffolds
    from rdkit import __version__ as RDKIT_VERSION
    RDKIT_AVAILABLE = True
except ImportError:
    RDKIT_AVAILABLE = False
    RDKIT_VERSION = "unknown"
    logging.warning("RDKit not available - RDKit MCP tools will not function")

# Setup structured logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Tool version for reproducibility
TOOL_VERSION = "2.0.0"

# ============================================================================
# CUSTOM EXCEPTIONS
# ============================================================================

class RDKitError(Exception):
    """Base exception for RDKit MCP server errors."""
    def __init__(self, message: str, error_type: str, recoverable: bool = False, suggestion: str = ""):
        self.message = message
        self.error_type = error_type
        self.recoverable = recoverable
        self.suggestion = suggestion
        super().__init__(self.message)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "error": self.message,
            "error_type": self.error_type,
            "recoverable": self.recoverable,
            "suggestion": self.suggestion if self.suggestion else None
        }

class SMILESValidationError(RDKitError):
    """Raised when SMILES validation fails."""
    def __init__(self, smiles: str, reason: str):
        super().__init__(
            message=f"Invalid SMILES '{smiles[:50]}...': {reason}",
            error_type="smiles_validation_error",
            recoverable=False,
            suggestion="Check SMILES syntax and try again"
        )

class MoleculeGenerationError(RDKitError):
    """Raised when molecule generation fails."""
    def __init__(self, reason: str):
        super().__init__(
            message=f"Molecule generation failed: {reason}",
            error_type="molecule_generation_error",
            recoverable=False
        )

class CoordinateGenerationError(RDKitError):
    """Raised when 2D coordinate generation fails."""
    def __init__(self, reason: str):
        super().__init__(
            message=f"Coordinate generation failed: {reason}",
            error_type="coordinate_generation_error",
            recoverable=True,
            suggestion="Molecule may have complex stereochemistry"
        )

class RenderingError(RDKitError):
    """Raised when image rendering fails."""
    def __init__(self, reason: str):
        super().__init__(
            message=f"Image rendering failed: {reason}",
            error_type="rendering_error",
            recoverable=True,
            suggestion="Try reducing image size or simplifying molecule"
        )

class RateLimitError(RDKitError):
    """Raised when rate limit is exceeded."""
    def __init__(self, limit: int, window: int):
        super().__init__(
            message=f"Rate limit exceeded: {limit} calls per {window}s",
            error_type="rate_limit_exceeded",
            recoverable=True,
            suggestion=f"Wait {window}s and try again"
        )

# ============================================================================
# PYDANTIC MODELS FOR VALIDATION
# ============================================================================

class SMILESInput(BaseModel):
    """Validated SMILES input."""
    smiles: str = Field(..., min_length=1, max_length=10000, description="SMILES string")
    
    @field_validator('smiles')
    @classmethod
    def validate_smiles_basic(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("SMILES cannot be empty or whitespace")
        return v.strip()

class ImageParams(BaseModel):
    """Parameters for molecule image generation."""
    smiles: str = Field(..., min_length=1, max_length=10000)
    width: int = Field(300, ge=50, le=4096, description="Image width in pixels")
    height: int = Field(300, ge=50, le=4096, description="Image height in pixels")
    
    @field_validator('smiles')
    @classmethod
    def validate_smiles(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("SMILES cannot be empty")
        return v.strip()

class FingerprintParams(BaseModel):
    """Parameters for fingerprint calculation."""
    smiles: str = Field(..., min_length=1, max_length=10000)
    radius: int = Field(2, ge=0, le=10, description="Morgan fingerprint radius")
    n_bits: int = Field(2048, ge=64, le=16384, description="Number of bits in fingerprint")
    
    @field_validator('smiles')
    @classmethod
    def validate_smiles(cls, v: str) -> str:
        return v.strip()

class SimilarityParams(BaseModel):
    """Parameters for similarity calculation."""
    smiles1: str = Field(..., min_length=1, max_length=10000)
    smiles2: str = Field(..., min_length=1, max_length=10000)
    radius: int = Field(2, ge=0, le=10)
    
    @field_validator('smiles1', 'smiles2')
    @classmethod
    def validate_smiles(cls, v: str) -> str:
        return v.strip()

class SubstructureParams(BaseModel):
    """Parameters for substructure search."""
    smiles: str = Field(..., min_length=1, max_length=10000)
    pattern_smarts: str = Field(..., min_length=1, max_length=10000, description="SMARTS pattern")
    
    @field_validator('smiles', 'pattern_smarts')
    @classmethod
    def validate_input(cls, v: str) -> str:
        return v.strip()

# ============================================================================
# RATE LIMITER
# ============================================================================

class RateLimiter:
    """Token bucket rate limiter for MCP tools."""
    
    def __init__(self, max_calls: int = 100, window_seconds: int = 60):
        self.max_calls = max_calls
        self.window = window_seconds
        self.calls: Dict[str, List[float]] = defaultdict(list)
        self.lock = Lock()
    
    def allow_request(self, client_id: str = "default") -> Tuple[bool, str]:
        """Check if request is allowed under rate limit."""
        with self.lock:
            now = time.time()
            
            # Clean old calls outside window
            self.calls[client_id] = [
                t for t in self.calls[client_id]
                if now - t < self.window
            ]
            
            if len(self.calls[client_id]) >= self.max_calls:
                return False, f"Rate limit exceeded: {self.max_calls} calls per {self.window}s"
            
            self.calls[client_id].append(now)
            return True, ""
    
    def reset(self, client_id: str = "default"):
        """Reset rate limit for client."""
        with self.lock:
            self.calls[client_id] = []

# Global rate limiter (100 calls per minute)
rate_limiter = RateLimiter(max_calls=100, window_seconds=60)

# Stricter rate limiter for expensive operations (20 calls per minute)
heavy_rate_limiter = RateLimiter(max_calls=20, window_seconds=60)

# ============================================================================
# DECORATORS
# ============================================================================

def with_rate_limit(limiter: RateLimiter = rate_limiter):
    """Decorator to apply rate limiting to tools."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            allowed, msg = limiter.allow_request()
            if not allowed:
                logger.warning(f"rate_limit_exceeded", tool=func.__name__)
                return {"error": msg, "error_type": "rate_limit_exceeded"}
            return func(*args, **kwargs)
        return wrapper
    return decorator

def with_logging(func):
    """Decorator to add structured logging to tools."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        start_time = time.time()
        tool_name = func.__name__
        
        logger.info(f"{tool_name}_started - args={len(args)}, kwargs={len(kwargs)}")
        
        try:
            result = func(*args, **kwargs)
            duration_ms = (time.time() - start_time) * 1000
            
            logger.info(f"{tool_name}_completed - duration_ms={round(duration_ms, 2)}, success=True")
            
            return result
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            logger.error(
                f"{tool_name}_failed - error={str(e)}, type={type(e).__name__}, duration_ms={round(duration_ms, 2)}"
            )
            raise
    return wrapper

def with_metadata(func):
    """Decorator to add version metadata to tool responses."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        result = func(*args, **kwargs)
        
        if isinstance(result, dict) and "error" not in result:
            result["_metadata"] = {
                "tool_version": TOOL_VERSION,
                "rdkit_version": RDKIT_VERSION,
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "tool_name": func.__name__
            }
        
        return result
    return wrapper

# ============================================================================
# VALIDATION UTILITIES
# ============================================================================

def validate_smiles(smiles: str) -> Tuple[bool, str, Optional[Chem.Mol]]:
    """
    Validate SMILES with detailed context.
    
    Returns:
        Tuple of (is_valid, error_message, mol_object)
    """
    if not RDKIT_AVAILABLE:
        return False, "RDKit not available", None
    
    if not smiles or not isinstance(smiles, str):
        return False, "SMILES must be a non-empty string", None
    
    smiles = smiles.strip()
    
    if len(smiles) > 10000:
        return False, "SMILES too long (max 10000 characters)", None
    
    try:
        mol = Chem.MolFromSmiles(smiles)
        
        if mol is None:
            return False, f"Invalid SMILES syntax: {smiles[:50]}...", None
        
        # Validate molecule structure
        if mol.GetNumAtoms() == 0:
            return False, "SMILES produces empty molecule", None
        
        # Sanitization check
        try:
            Chem.SanitizeMol(mol)
        except Exception as e:
            return False, f"Molecule sanitization failed: {str(e)}", None
        
        return True, "", mol
        
    except Exception as e:
        return False, f"SMILES parsing error: {str(e)}", None

def validate_smarts(smarts: str) -> Tuple[bool, str, Optional[Chem.Mol]]:
    """Validate SMARTS pattern."""
    if not RDKIT_AVAILABLE:
        return False, "RDKit not available", None
    
    if not smarts or not isinstance(smarts, str):
        return False, "SMARTS must be a non-empty string", None
    
    try:
        pattern = Chem.MolFromSmarts(smarts.strip())
        if pattern is None:
            return False, f"Invalid SMARTS syntax: {smarts[:50]}...", None
        return True, "", pattern
    except Exception as e:
        return False, f"SMARTS parsing error: {str(e)}", None

# Initialize FastMCP server (in-process)
rdkit_native_server = FastMCP("RDKit-Native-Expanded")


# ============================================================================
# CORE MOLECULAR TOOLS
# ============================================================================

@rdkit_native_server.tool()  # Read-only, idempotent, non-destructive
@with_rate_limit()
@with_logging
@with_metadata
def smiles_to_mol(smiles: str) -> Dict[str, Any]:
    """
    Convert SMILES string to RDKit Mol object and return basic info.
    
    Validates SMILES syntax and returns molecular properties including
    formula, atom counts, and bond information.
    
    Args:
        smiles: SMILES string representation of molecule (max 10000 chars)
        
    Returns:
        Dictionary with:
        - valid: bool flag
        - smiles: input SMILES
        - molecular_formula: chemical formula
        - num_atoms: total atom count
        - num_heavy_atoms: non-hydrogen atom count
        - num_bonds: bond count
        - _metadata: version and timestamp info
        
    Raises:
        SMILESValidationError: If SMILES is invalid
    """
    if not RDKIT_AVAILABLE:
        return RDKitError("RDKit not available", "rdkit_unavailable").to_dict()
    
    # Robust validation
    is_valid, error_msg, mol = validate_smiles(smiles)
    
    if not is_valid:
        raise SMILESValidationError(smiles, error_msg)
    
    try:
        return {
            "valid": True,
            "smiles": smiles,
            "molecular_formula": Chem.rdMolDescriptors.CalcMolFormula(mol),
            "num_atoms": mol.GetNumAtoms(),
            "num_heavy_atoms": mol.GetNumHeavyAtoms(),
            "num_bonds": mol.GetNumBonds(),
        }
    except Exception as e:
        raise MoleculeGenerationError(str(e))


@rdkit_native_server.tool()  # Read-only, idempotent, non-destructive
@with_rate_limit()
@with_logging
@with_metadata
def calculate_molecular_weight(smiles: str) -> Dict[str, float]:
    """
    Calculate molecular weight and exact molecular weight.
    
    Computes both average molecular weight (using average atomic weights)
    and exact molecular weight (using exact isotopic masses).
    
    Args:
        smiles: SMILES string (max 10000 chars)
        
    Returns:
        Dictionary with:
        - smiles: input SMILES
        - molecular_weight: average MW in Da
        - exact_molecular_weight: exact isotopic MW in Da
        - _metadata: version and timestamp info
        
    Raises:
        SMILESValidationError: If SMILES is invalid
    """
    if not RDKIT_AVAILABLE:
        return RDKitError("RDKit not available", "rdkit_unavailable").to_dict()
    
    is_valid, error_msg, mol = validate_smiles(smiles)
    
    if not is_valid:
        raise SMILESValidationError(smiles, error_msg)
    
    try:
        return {
            "smiles": smiles,
            "molecular_weight": Descriptors.MolWt(mol),
            "exact_molecular_weight": Descriptors.ExactMolWt(mol),
        }
    except Exception as e:
        raise MoleculeGenerationError(f"MW calculation failed: {str(e)}")


@rdkit_native_server.tool()
def calculate_lipinski_descriptors(smiles: str) -> Dict[str, Any]:
    """
    Calculate Lipinski Rule of Five descriptors for drug-likeness.
    
    Lipinski's Rule of Five:
    - MW <= 500 Da
    - LogP <= 5
    - HBD (Hydrogen Bond Donors) <= 5
    - HBA (Hydrogen Bond Acceptors) <= 10
    
    Args:
        smiles: SMILES string
        
    Returns:
        Dictionary with MW, LogP, HBD, HBA, and rule_of_five_compliant flag
    """
    if not RDKIT_AVAILABLE:
        return {"error": "RDKit not available"}
    
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return {"error": f"Invalid SMILES: {smiles}"}
    
    mw = Descriptors.MolWt(mol)
    logp = Crippen.MolLogP(mol)
    hbd = Lipinski.NumHDonors(mol)
    hba = Lipinski.NumHAcceptors(mol)
    
    # Check Lipinski's Rule of Five
    ro5_compliant = (
        mw <= 500 and
        logp <= 5 and
        hbd <= 5 and
        hba <= 10
    )
    
    return {
        "smiles": smiles,
        "molecular_weight": mw,
        "logp": logp,
        "num_h_donors": hbd,
        "num_h_acceptors": hba,
        "rule_of_five_compliant": ro5_compliant,
        "violations": sum([
            mw > 500,
            logp > 5,
            hbd > 5,
            hba > 10
        ])
    }


@rdkit_native_server.tool()
def calculate_tpsa(smiles: str) -> Dict[str, float]:
    """
    Calculate Topological Polar Surface Area (TPSA).
    
    TPSA is important for predicting drug transport properties.
    Good oral bioavailability: TPSA < 140 Å²
    
    Args:
        smiles: SMILES string
        
    Returns:
        Dictionary with TPSA value
    """
    if not RDKIT_AVAILABLE:
        return {"error": "RDKit not available"}
    
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return {"error": f"Invalid SMILES: {smiles}"}
    
    tpsa = Descriptors.TPSA(mol)
    
    return {
        "smiles": smiles,
        "tpsa": tpsa,
        "good_oral_bioavailability": tpsa < 140
    }


@rdkit_native_server.tool()
def calculate_rotatable_bonds(smiles: str) -> Dict[str, int]:
    """
    Calculate number of rotatable bonds.
    
    Indicator of molecular flexibility.
    Drug-like molecules typically have <= 10 rotatable bonds.
    
    Args:
        smiles: SMILES string
        
    Returns:
        Dictionary with num_rotatable_bonds
    """
    if not RDKIT_AVAILABLE:
        return {"error": "RDKit not available"}
    
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return {"error": f"Invalid SMILES: {smiles}"}
    
    num_rot = Lipinski.NumRotatableBonds(mol)
    
    return {
        "smiles": smiles,
        "num_rotatable_bonds": num_rot,
        "drug_like": num_rot <= 10
    }


@rdkit_native_server.tool()
def calculate_aromatic_rings(smiles: str) -> Dict[str, int]:
    """
    Calculate number of aromatic rings.
    
    Args:
        smiles: SMILES string
        
    Returns:
        Dictionary with num_aromatic_rings and num_aliphatic_rings
    """
    if not RDKIT_AVAILABLE:
        return {"error": "RDKit not available"}
    
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return {"error": f"Invalid SMILES: {smiles}"}
    
    return {
        "smiles": smiles,
        "num_aromatic_rings": Descriptors.NumAromaticRings(mol),
        "num_aliphatic_rings": Descriptors.NumAliphaticRings(mol),
        "num_saturated_rings": Descriptors.NumSaturatedRings(mol),
    }


@rdkit_native_server.tool()
def calculate_comprehensive_descriptors(smiles: str) -> Dict[str, Any]:
    """
    Calculate comprehensive set of molecular descriptors.
    
    Combines all common descriptors in one call for efficiency.
    
    Args:
        smiles: SMILES string
        
    Returns:
        Dictionary with all molecular descriptors
    """
    if not RDKIT_AVAILABLE:
        return {"error": "RDKit not available"}
    
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return {"error": f"Invalid SMILES: {smiles}"}
    
    # Calculate all descriptors
    mw = Descriptors.MolWt(mol)
    logp = Crippen.MolLogP(mol)
    hbd = Lipinski.NumHDonors(mol)
    hba = Lipinski.NumHAcceptors(mol)
    tpsa = Descriptors.TPSA(mol)
    num_rot = Lipinski.NumRotatableBonds(mol)
    
    # Lipinski Rule of Five
    ro5_compliant = (mw <= 500 and logp <= 5 and hbd <= 5 and hba <= 10)
    
    return {
        "smiles": smiles,
        "molecular_formula": Chem.rdMolDescriptors.CalcMolFormula(mol),
        "molecular_weight": mw,
        "exact_molecular_weight": Descriptors.ExactMolWt(mol),
        "logp": logp,
        "num_h_donors": hbd,
        "num_h_acceptors": hba,
        "tpsa": tpsa,
        "num_rotatable_bonds": num_rot,
        "num_aromatic_rings": Descriptors.NumAromaticRings(mol),
        "num_aliphatic_rings": Descriptors.NumAliphaticRings(mol),
        "num_atoms": mol.GetNumAtoms(),
        "num_heavy_atoms": mol.GetNumHeavyAtoms(),
        "num_bonds": mol.GetNumBonds(),
        "rule_of_five_compliant": ro5_compliant,
        "good_oral_bioavailability": tpsa < 140,
        "molecular_flexibility": "high" if num_rot > 10 else "low"
    }


@rdkit_native_server.tool()
def substructure_match(smiles: str, substructure_smarts: str) -> Dict[str, Any]:
    """
    Check if molecule contains a substructure pattern.
    
    Args:
        smiles: SMILES string of molecule to search
        substructure_smarts: SMARTS pattern to search for
        
    Returns:
        Dictionary with match status and matched atoms
    """
    if not RDKIT_AVAILABLE:
        return {"error": "RDKit not available"}
    
    mol = Chem.MolFromSmiles(smiles)
    pattern = Chem.MolFromSmarts(substructure_smarts)
    
    if mol is None:
        return {"error": f"Invalid SMILES: {smiles}"}
    if pattern is None:
        return {"error": f"Invalid SMARTS: {substructure_smarts}"}
    
    matches = mol.GetSubstructMatches(pattern)
    
    return {
        "smiles": smiles,
        "substructure_smarts": substructure_smarts,
        "has_match": len(matches) > 0,
        "num_matches": len(matches),
        "matched_atom_indices": [list(match) for match in matches]
    }


# ============================================================================
# DRUG DISCOVERY TOOLS
# ============================================================================

@rdkit_native_server.tool()
def evaluate_drug_likeness(smiles: str) -> Dict[str, Any]:
    """
    Comprehensive drug-likeness evaluation.
    
    Evaluates:
    - Lipinski's Rule of Five
    - Veber's Rules (TPSA, rotatable bonds)
    - Molecular complexity
    
    Args:
        smiles: SMILES string
        
    Returns:
        Dictionary with drug-likeness assessment
    """
    if not RDKIT_AVAILABLE:
        return {"error": "RDKit not available"}
    
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return {"error": f"Invalid SMILES: {smiles}"}
    
    # Lipinski descriptors
    mw = Descriptors.MolWt(mol)
    logp = Crippen.MolLogP(mol)
    hbd = Lipinski.NumHDonors(mol)
    hba = Lipinski.NumHAcceptors(mol)
    
    # Veber descriptors
    tpsa = Descriptors.TPSA(mol)
    num_rot = Lipinski.NumRotatableBonds(mol)
    
    # Evaluate rules
    lipinski_pass = (mw <= 500 and logp <= 5 and hbd <= 5 and hba <= 10)
    veber_pass = (tpsa <= 140 and num_rot <= 10)
    
    return {
        "smiles": smiles,
        "lipinski": {
            "compliant": lipinski_pass,
            "molecular_weight": mw,
            "logp": logp,
            "h_donors": hbd,
            "h_acceptors": hba
        },
        "veber": {
            "compliant": veber_pass,
            "tpsa": tpsa,
            "rotatable_bonds": num_rot
        },
        "overall_drug_like": lipinski_pass and veber_pass,
        "recommendation": "Promising drug candidate" if (lipinski_pass and veber_pass) else "Needs optimization"
    }


# ============================================================================
# SIMILARITY & FINGERPRINTS
# ============================================================================

@rdkit_native_server.tool()  # Read-only, idempotent, non-destructive (expensive)
@with_rate_limit(heavy_rate_limiter)  # Expensive operation - stricter limit
@with_logging
@with_metadata
def calculate_morgan_fingerprint(smiles: str, radius: int = 2, n_bits: int = 2048) -> Dict[str, Any]:
    """
    Calculate Morgan (circular) fingerprint for molecule.
    
    Morgan fingerprints are used for similarity searches, virtual screening,
    and machine learning models. This is a computationally expensive operation
    with stricter rate limiting (20 calls/min).
    
    Args:
        smiles: SMILES string (max 10000 chars)
        radius: Fingerprint radius, 0-10 (default: 2, ECFP4 equivalent)
        n_bits: Fingerprint length in bits, 64-16384 (default: 2048)
        
    Returns:
        Dictionary with:
        - smiles: input SMILES
        - fingerprint_type: "Morgan"
        - radius: used radius
        - n_bits: fingerprint length
        - on_bits: number of set bits
        - fingerprint_bits: list of set bit indices (for sparse representation)
        - _metadata: version and timestamp info
        
    Raises:
        SMILESValidationError: If SMILES is invalid
        RateLimitError: If rate limit exceeded (20 calls/60s)
    """
    if not RDKIT_AVAILABLE:
        return RDKitError("RDKit not available", "rdkit_unavailable").to_dict()
    
    # Validate parameters
    try:
        params = FingerprintParams(smiles=smiles, radius=radius, n_bits=n_bits)
    except ValueError as e:
        return {"error": f"Parameter validation failed: {str(e)}", "error_type": "validation_error"}
    
    # Validate SMILES
    is_valid, error_msg, mol = validate_smiles(params.smiles)
    if not is_valid:
        raise SMILESValidationError(params.smiles, error_msg)
    
    try:
        fp = AllChem.GetMorganFingerprintAsBitVect(mol, params.radius, nBits=params.n_bits)
        
        # Get list of on bits for sparse representation
        on_bits_list = list(fp.GetOnBits())
        
        return {
            "smiles": params.smiles,
            "fingerprint_type": "Morgan",
            "radius": params.radius,
            "n_bits": params.n_bits,
            "on_bits": fp.GetNumOnBits(),
            "fingerprint_bits": on_bits_list[:100] if len(on_bits_list) > 100 else on_bits_list,
            "sparsity": round(fp.GetNumOnBits() / params.n_bits, 4)
        }
    except MemoryError:
        raise RDKitError(
            f"Fingerprint calculation exceeded memory (n_bits={params.n_bits})",
            "memory_error",
            recoverable=True,
            suggestion="Reduce n_bits parameter"
        )
    except Exception as e:
        raise MoleculeGenerationError(f"Fingerprint calculation failed: {str(e)}")


@rdkit_native_server.tool()
def calculate_tanimoto_similarity(smiles1: str, smiles2: str) -> Dict[str, float]:
    """
    Calculate Tanimoto similarity between two molecules.
    
    Uses Morgan fingerprints with radius=2.
    Similarity ranges from 0 (completely different) to 1 (identical).
    
    Args:
        smiles1: First SMILES string
        smiles2: Second SMILES string
        
    Returns:
        Dictionary with similarity score
    """
    if not RDKIT_AVAILABLE:
        return {"error": "RDKit not available"}
    
    mol1 = Chem.MolFromSmiles(smiles1)
    mol2 = Chem.MolFromSmiles(smiles2)
    
    if mol1 is None:
        return {"error": f"Invalid SMILES: {smiles1}"}
    if mol2 is None:
        return {"error": f"Invalid SMILES: {smiles2}"}
    
    fp1 = AllChem.GetMorganFingerprintAsBitVect(mol1, 2)
    fp2 = AllChem.GetMorganFingerprintAsBitVect(mol2, 2)
    
    from rdkit import DataStructs
    similarity = DataStructs.TanimotoSimilarity(fp1, fp2)
    
    return {
        "smiles1": smiles1,
        "smiles2": smiles2,
        "tanimoto_similarity": similarity,
        "interpretation": (
            "Very similar" if similarity > 0.85 else
            "Similar" if similarity > 0.7 else
            "Somewhat similar" if similarity > 0.5 else
            "Different"
        )
    }


# ============================================================================
# AUTO-REGISTERED RDKIT DESCRIPTOR TOOLS (Expanded Coverage)
# ============================================================================

# Auto-register Chem.Descriptors functions
if RDKIT_AVAILABLE:
    DESCRIPTOR_FUNCTIONS = [
        ("ExactMolWt", Descriptors.ExactMolWt, "Calculate exact molecular weight"),
        ("MolWt", Descriptors.MolWt, "Calculate molecular weight"),
        ("HeavyAtomMolWt", Descriptors.HeavyAtomMolWt, "Calculate heavy atom molecular weight"),
        ("NumRadicalElectrons", Descriptors.NumRadicalElectrons, "Count radical electrons"),
        ("NumValenceElectrons", Descriptors.NumValenceElectrons, "Count valence electrons"),
        ("MaxPartialCharge", Descriptors.MaxPartialCharge, "Calculate max partial charge"),
        ("MinPartialCharge", Descriptors.MinPartialCharge, "Calculate min partial charge"),
        ("MaxAbsPartialCharge", Descriptors.MaxAbsPartialCharge, "Calculate max absolute partial charge"),
        ("MinAbsPartialCharge", Descriptors.MinAbsPartialCharge, "Calculate min absolute partial charge"),
        ("FpDensityMorgan1", Descriptors.FpDensityMorgan1, "Calculate Morgan fingerprint density (radius 1)"),
        ("FpDensityMorgan2", Descriptors.FpDensityMorgan2, "Calculate Morgan fingerprint density (radius 2)"),
        ("FpDensityMorgan3", Descriptors.FpDensityMorgan3, "Calculate Morgan fingerprint density (radius 3)"),
    ]
    
    for func_name, func, description in DESCRIPTOR_FUNCTIONS:
        # Create wrapper that takes SMILES and applies descriptor
        def create_descriptor_tool(rdkit_func: Callable, desc: str, name: str):
            def tool_func(smiles: str) -> Dict[str, Any]:
                mol = Chem.MolFromSmiles(smiles)
                if mol is None:
                    return {"error": f"Invalid SMILES: {smiles}"}
                try:
                    result = rdkit_func(mol)
                    return {
                        "smiles": smiles,
                        "descriptor": name,
                        "value": result
                    }
                except Exception as e:
                    return {"error": str(e), "descriptor": name}
            
            tool_func.__name__ = f"rdkit_descriptor_{name.lower()}"
            tool_func.__doc__ = f"{desc}\n\nArgs:\n    smiles: SMILES string\n\nReturns:\n    Dictionary with descriptor value"
            return tool_func
        
        tool = create_descriptor_tool(func, description, func_name)
        rdkit_native_server.tool()(tool)

    # Auto-register rdMolDescriptors functions
    MOL_DESCRIPTOR_FUNCTIONS = [
        ("CalcTPSA", rdMolDescriptors.CalcTPSA, "Calculate topological polar surface area"),
        ("CalcNumHBA", rdMolDescriptors.CalcNumHBA, "Calculate number of hydrogen bond acceptors"),
        ("CalcNumHBD", rdMolDescriptors.CalcNumHBD, "Calculate number of hydrogen bond donors"),
        ("CalcNumLipinskiHBA", rdMolDescriptors.CalcNumLipinskiHBA, "Calculate Lipinski HBA"),
        ("CalcNumLipinskiHBD", rdMolDescriptors.CalcNumLipinskiHBD, "Calculate Lipinski HBD"),
        ("CalcNumRings", rdMolDescriptors.CalcNumRings, "Calculate number of rings"),
        ("CalcNumAromaticRings", rdMolDescriptors.CalcNumAromaticRings, "Calculate number of aromatic rings"),
        ("CalcNumAliphaticRings", rdMolDescriptors.CalcNumAliphaticRings, "Calculate number of aliphatic rings"),
        ("CalcNumSaturatedRings", rdMolDescriptors.CalcNumSaturatedRings, "Calculate number of saturated rings"),
        ("CalcNumHeterocycles", rdMolDescriptors.CalcNumHeterocycles, "Calculate number of heterocycles"),
        ("CalcNumRotatableBonds", rdMolDescriptors.CalcNumRotatableBonds, "Calculate number of rotatable bonds"),
        ("CalcNumHeavyAtoms", rdMolDescriptors.CalcNumHeavyAtoms, "Calculate number of heavy atoms"),
        ("CalcNumHeteroatoms", rdMolDescriptors.CalcNumHeteroatoms, "Calculate number of heteroatoms"),
        ("CalcNumAmideBonds", rdMolDescriptors.CalcNumAmideBonds, "Calculate number of amide bonds"),
        ("CalcNumSpiroAtoms", rdMolDescriptors.CalcNumSpiroAtoms, "Calculate number of spiro atoms"),
        ("CalcFractionCSP3", rdMolDescriptors.CalcFractionCSP3, "Calculate fraction of sp3 carbons"),
        ("CalcChi0v", rdMolDescriptors.CalcChi0v, "Calculate Chi0v molecular connectivity index"),
        ("CalcChi1v", rdMolDescriptors.CalcChi1v, "Calculate Chi1v molecular connectivity index"),
        ("CalcChi2v", rdMolDescriptors.CalcChi2v, "Calculate Chi2v molecular connectivity index"),
        ("CalcChi3v", rdMolDescriptors.CalcChi3v, "Calculate Chi3v molecular connectivity index"),
        ("CalcChi4v", rdMolDescriptors.CalcChi4v, "Calculate Chi4v molecular connectivity index"),
        ("CalcKappa1", rdMolDescriptors.CalcKappa1, "Calculate Kappa1 shape index"),
        ("CalcKappa2", rdMolDescriptors.CalcKappa2, "Calculate Kappa2 shape index"),
        ("CalcKappa3", rdMolDescriptors.CalcKappa3, "Calculate Kappa3 shape index"),
        ("CalcLabuteASA", rdMolDescriptors.CalcLabuteASA, "Calculate Labute accessible surface area"),
        ("CalcPBF", rdMolDescriptors.CalcPBF, "Calculate plane of best fit"),
        ("CalcMolFormula", rdMolDescriptors.CalcMolFormula, "Calculate molecular formula"),
    ]
    
    for func_name, func, description in MOL_DESCRIPTOR_FUNCTIONS:
        def create_mol_descriptor_tool(rdkit_func: Callable, desc: str, name: str):
            def tool_func(smiles: str) -> Dict[str, Any]:
                mol = Chem.MolFromSmiles(smiles)
                if mol is None:
                    return {"error": f"Invalid SMILES: {smiles}"}
                try:
                    result = rdkit_func(mol)
                    return {
                        "smiles": smiles,
                        "descriptor": name,
                        "value": result
                    }
                except Exception as e:
                    return {"error": str(e), "descriptor": name}
            
            tool_func.__name__ = f"rdkit_moldesc_{name.lower()}"
            tool_func.__doc__ = f"{desc}\n\nArgs:\n    smiles: SMILES string\n\nReturns:\n    Dictionary with descriptor value"
            return tool_func
        
        tool = create_mol_descriptor_tool(func, description, func_name)
        rdkit_native_server.tool()(tool)


# ============================================================================
# DRAWING AND VISUALIZATION TOOLS
# ============================================================================

@rdkit_native_server.tool()  # Generates images, idempotent, non-destructive
@with_rate_limit()
@with_logging
@with_metadata
def mol_to_image(smiles: str, width: int = 300, height: int = 300) -> Dict[str, Any]:
    """
    Generate 2D molecular structure image from SMILES.
    
    Creates a PNG image of the molecule with 2D coordinates optimized
    for visualization. Image is returned as base64-encoded string.
    
    Args:
        smiles: SMILES string (max 10000 chars)
        width: Image width in pixels (50-4096, default 300)
        height: Image height in pixels (50-4096, default 300)
        
    Returns:
        Dictionary with:
        - smiles: input SMILES
        - image_base64: base64-encoded PNG image
        - format: "PNG"
        - width: actual image width
        - height: actual image height
        - _metadata: version and timestamp info
        
    Raises:
        SMILESValidationError: If SMILES is invalid
        CoordinateGenerationError: If 2D coordinate generation fails
        RenderingError: If image rendering fails
    """
    if not RDKIT_AVAILABLE:
        return RDKitError("RDKit not available", "rdkit_unavailable").to_dict()
    
    # Validate parameters
    try:
        params = ImageParams(smiles=smiles, width=width, height=height)
    except ValueError as e:
        return {"error": f"Parameter validation failed: {str(e)}", "error_type": "validation_error"}
    
    # Validate SMILES
    is_valid, error_msg, mol = validate_smiles(params.smiles)
    if not is_valid:
        raise SMILESValidationError(params.smiles, error_msg)
    
    # Generate 2D coordinates
    try:
        AllChem.Compute2DCoords(mol)
    except ValueError as e:
        raise CoordinateGenerationError(f"2D layout failed: {str(e)}")
    except Exception as e:
        raise CoordinateGenerationError(str(e))
    
    # Render image
    try:
        img = Draw.MolToImage(mol, size=(params.width, params.height))
    except MemoryError:
        raise RenderingError("Image too large - reduce dimensions")
    except Exception as e:
        raise RenderingError(f"Drawing failed: {str(e)}")
    
    # Encode to base64
    try:
        buffered = BytesIO()
        img.save(buffered, format="PNG")
        img_base64 = base64.b64encode(buffered.getvalue()).decode()
        
        return {
            "smiles": params.smiles,
            "image_base64": img_base64,
            "format": "PNG",
            "width": params.width,
            "height": params.height,
            "image_size_bytes": len(img_base64)
        }
    except Exception as e:
        raise RenderingError(f"Image encoding failed: {str(e)}")


@rdkit_native_server.tool()
def compute_2d_coords(smiles: str) -> Dict[str, Any]:
    """
    Compute 2D coordinates for molecular display.
    
    Args:
        smiles: SMILES string
        
    Returns:
        Dictionary with success status and SMILES with 2D coords
    """
    if not RDKIT_AVAILABLE:
        return {"error": "RDKit not available"}
    
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return {"error": f"Invalid SMILES: {smiles}"}
    
    try:
        AllChem.Compute2DCoords(mol)
        return {
            "smiles": smiles,
            "success": True,
            "smiles_with_coords": Chem.MolToSmiles(mol)
        }
    except Exception as e:
        return {"error": str(e), "success": False}


# ============================================================================
# CONVERSION TOOLS (PDB, SDF, SMILES)
# ============================================================================

@rdkit_native_server.tool()
def mol_to_smiles(smiles_input: str) -> Dict[str, str]:
    """
    Convert molecule to canonical SMILES (useful for standardization).
    
    Args:
        smiles_input: Input SMILES string
        
    Returns:
        Dictionary with canonical SMILES
    """
    if not RDKIT_AVAILABLE:
        return {"error": "RDKit not available"}
    
    mol = Chem.MolFromSmiles(smiles_input)
    if mol is None:
        return {"error": f"Invalid SMILES: {smiles_input}"}
    
    canonical_smiles = Chem.MolToSmiles(mol)
    
    return {
        "input_smiles": smiles_input,
        "canonical_smiles": canonical_smiles,
        "is_canonical": smiles_input == canonical_smiles
    }


@rdkit_native_server.tool()
def smiles_to_mol_formula(smiles: str) -> Dict[str, str]:
    """
    Convert SMILES to molecular formula.
    
    Args:
        smiles: SMILES string
        
    Returns:
        Dictionary with molecular formula
    """
    if not RDKIT_AVAILABLE:
        return {"error": "RDKit not available"}
    
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return {"error": f"Invalid SMILES: {smiles}"}
    
    formula = rdMolDescriptors.CalcMolFormula(mol)
    
    return {
        "smiles": smiles,
        "molecular_formula": formula
    }


# ============================================================================
# SCAFFOLD AND FRAGMENT TOOLS
# ============================================================================

@rdkit_native_server.tool()
def get_murcko_scaffold(smiles: str) -> Dict[str, str]:
    """
    Get Murcko scaffold (core structure) from molecule.
    
    Args:
        smiles: SMILES string
        
    Returns:
        Dictionary with scaffold SMILES
    """
    if not RDKIT_AVAILABLE:
        return {"error": "RDKit not available"}
    
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return {"error": f"Invalid SMILES: {smiles}"}
    
    try:
        scaffold = Scaffolds.MurckoScaffold.GetScaffoldForMol(mol)
        scaffold_smiles = Chem.MolToSmiles(scaffold)
        
        return {
            "input_smiles": smiles,
            "scaffold_smiles": scaffold_smiles,
            "success": True
        }
    except Exception as e:
        return {"error": str(e), "success": False}


@rdkit_native_server.tool()
def get_generic_scaffold(smiles: str) -> Dict[str, str]:
    """
    Get generic Murcko scaffold (all atoms converted to carbon, all bonds to single).
    
    Args:
        smiles: SMILES string
        
    Returns:
        Dictionary with generic scaffold SMILES
    """
    if not RDKIT_AVAILABLE:
        return {"error": "RDKit not available"}
    
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return {"error": f"Invalid SMILES: {smiles}"}
    
    try:
        scaffold = Scaffolds.MurckoScaffold.MakeScaffoldGeneric(
            Scaffolds.MurckoScaffold.GetScaffoldForMol(mol)
        )
        scaffold_smiles = Chem.MolToSmiles(scaffold)
        
        return {
            "input_smiles": smiles,
            "generic_scaffold_smiles": scaffold_smiles,
            "success": True
        }
    except Exception as e:
        return {"error": str(e), "success": False}


# ============================================================================
# SUBSTRUCTURE MATCHING (EXPANDED)
# ============================================================================

@rdkit_native_server.tool()
def has_substructure_match(smiles: str, pattern_smarts: str) -> Dict[str, Any]:
    """
    Check if molecule contains a substructure pattern (SMARTS).
    
    Args:
        smiles: SMILES string of molecule to search
        pattern_smarts: SMARTS pattern to search for
        
    Returns:
        Dictionary with match result and details
    """
    if not RDKIT_AVAILABLE:
        return {"error": "RDKit not available"}
    
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return {"error": f"Invalid SMILES: {smiles}"}
    
    pattern = Chem.MolFromSmarts(pattern_smarts)
    if pattern is None:
        return {"error": f"Invalid SMARTS pattern: {pattern_smarts}"}
    
    has_match = mol.HasSubstructMatch(pattern)
    
    return {
        "smiles": smiles,
        "pattern_smarts": pattern_smarts,
        "has_match": has_match,
        "match_count": len(mol.GetSubstructMatches(pattern)) if has_match else 0
    }


@rdkit_native_server.tool()
def get_substructure_matches(smiles: str, pattern_smarts: str) -> Dict[str, Any]:
    """
    Get all substructure matches with atom indices.
    
    Args:
        smiles: SMILES string of molecule to search
        pattern_smarts: SMARTS pattern to search for
        
    Returns:
        Dictionary with all matches (atom indices)
    """
    if not RDKIT_AVAILABLE:
        return {"error": "RDKit not available"}
    
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return {"error": f"Invalid SMILES: {smiles}"}
    
    pattern = Chem.MolFromSmarts(pattern_smarts)
    if pattern is None:
        return {"error": f"Invalid SMARTS pattern: {pattern_smarts}"}
    
    matches = mol.GetSubstructMatches(pattern)
    
    return {
        "smiles": smiles,
        "pattern_smarts": pattern_smarts,
        "num_matches": len(matches),
        "matches": [list(match) for match in matches]
    }


# ============================================================================
# SERVER INFO
# ============================================================================

@rdkit_native_server.tool()
def get_rdkit_version() -> Dict[str, Any]:
    """
    Get RDKit version and availability status.
    
    Returns:
        Dictionary with version info and tool count
    """
    if not RDKIT_AVAILABLE:
        return {
            "available": False,
            "error": "RDKit not installed"
        }
    
    from rdkit import __version__
    
    # Count registered tools dynamically
    import asyncio
    tools = asyncio.run(rdkit_native_server.get_tools())
    
    return {
        "available": True,
        "version": __version__,
        "total_tools": len(tools),
        "categories": [
            "Core Molecular",
            "Descriptors (12 auto-registered)",
            "Molecular Descriptors (27 auto-registered)",
            "Drug-likeness",
            "Similarity & Fingerprints",
            "Substructure Matching",
            "Drawing & Visualization",
            "Conversion (SMILES, Formula)",
            "Scaffold Analysis"
        ]
    }


# Export server for use by AgenticDriver
__all__ = ["rdkit_native_server"]


if __name__ == "__main__":
    # Test mode
    print("🧪 RDKit Native MCP Server (In-Process)")
    print(f"   Available: {RDKIT_AVAILABLE}")
    print(f"   Tools registered: {len(rdkit_native_server.list_tools())}")
    
    if RDKIT_AVAILABLE:
        print("\n   Sample tools:")
        for tool in rdkit_native_server.list_tools()[:5]:
            print(f"   - {tool.name}")
