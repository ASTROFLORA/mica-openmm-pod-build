"""
Validation Module for HPC Batch Processing
===========================================

Author: Alex Rodriguez
Date: October 8, 2025
"""

from dataclasses import dataclass, asdict
from typing import List, Dict, Any, Optional
from datetime import datetime
import pandas as pd


@dataclass
class IngestionReport:
    """Report for batch ingestion results"""
    
    total_proteins: int
    successful: int
    failed: int
    skipped: int
    duration_seconds: float
    throughput: float  # proteins per second
    errors: List[Dict[str, Any]]
    timestamp: datetime
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert report to dictionary"""
        data = asdict(self)
        data["timestamp"] = self.timestamp.isoformat()
        return data
    
    def summary(self) -> str:
        """Generate human-readable summary"""
        success_rate = (self.successful / self.total_proteins * 100) if self.total_proteins > 0 else 0
        
        return f"""
Batch Ingestion Report
======================
Total Proteins: {self.total_proteins}
Successful: {self.successful} ({success_rate:.1f}%)
Failed: {self.failed}
Skipped: {self.skipped}
Duration: {self.duration_seconds:.1f}s
Throughput: {self.throughput:.2f} proteins/sec
Timestamp: {self.timestamp.isoformat()}
"""


class BatchValidator:
    """Validator for batch processing inputs and outputs"""
    
    def validate_input_csv(self, df: pd.DataFrame) -> List[str]:
        """
        Validate input CSV structure.
        
        Required columns:
        - uniprot_id: UniProt accession (e.g., P12345)
        - gene_symbol: Gene symbol (e.g., WNK1)
        - name: Protein name
        
        Returns:
            List of validation errors (empty if valid)
        """
        errors = []
        
        # Check required columns
        required_columns = {"uniprot_id", "gene_symbol", "name"}
        missing_columns = required_columns - set(df.columns)
        
        if missing_columns:
            errors.append(f"Missing required columns: {missing_columns}")
            return errors
        
        # Check for empty values
        for col in required_columns:
            if df[col].isna().any():
                null_count = df[col].isna().sum()
                errors.append(f"Column '{col}' has {null_count} null values")
        
        # Validate UniProt IDs format
        invalid_uniprot = df[~df["uniprot_id"].str.match(r'^[A-Z0-9]{6,10}$', na=False)]
        if len(invalid_uniprot) > 0:
            errors.append(f"Invalid UniProt IDs found: {invalid_uniprot['uniprot_id'].tolist()[:5]}")
        
        # Check for duplicates
        duplicates = df[df.duplicated(subset=["uniprot_id"], keep=False)]
        if len(duplicates) > 0:
            errors.append(f"Duplicate UniProt IDs found: {duplicates['uniprot_id'].tolist()[:5]}")
        
        return errors
    
    def validate_ingestion_results(self, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Validate ingestion results.
        
        Returns:
            Dictionary with validation metrics
        """
        total = len(results)
        success = sum(1 for r in results if r["status"] == "success")
        failed = sum(1 for r in results if r["status"] == "failed")
        
        # Check BUDO ID format for successful entries
        invalid_budo_ids = []
        for r in results:
            if r["status"] == "success" and r.get("budo_id"):
                if not r["budo_id"].startswith("budo:"):
                    invalid_budo_ids.append(r["budo_id"])
        
        return {
            "total": total,
            "success": success,
            "failed": failed,
            "success_rate": success / total if total > 0 else 0,
            "invalid_budo_ids": invalid_budo_ids,
            "is_valid": len(invalid_budo_ids) == 0
        }
