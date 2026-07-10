"""MICA-Lineage System Compatibility Checker.

This module provides an automated validation utility to verify that a target
environment satisfies the software and hardware requirements required to run
MICA-Lineage (Protocolo Fénix Azteca) workloads.  It is aligned with the
BSM-BUDO-CEA unified roadmap Phase 4.1 activities led by Dr. Yuan Chen and can
be executed as a standalone script or imported as a library utility.

Example
=======
>>> from bsm.mica_lineage.compatibility_checker import MICALineageCompatibilityChecker
>>> checker = MICALineageCompatibilityChecker()
>>> report = checker.check_system_compatibility()
>>> checker.save_report("mica_lineage_compatibility_report.json")
"""
from __future__ import annotations

import argparse
import importlib
import json
import logging
import platform
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from packaging.specifiers import SpecifierSet
from packaging.version import Version

try:  # psutil is preferred but optional; fall back gracefully.
    import psutil  # type: ignore
except Exception:  # pragma: no cover - defensive
    psutil = None  # type: ignore

try:  # torch is optional; availability is validated later.
    import torch  # type: ignore
except Exception:  # pragma: no cover - defensive
    torch = None  # type: ignore

# Reduce noisy warnings during capability checks (i.e., CUDA env probing).
warnings.filterwarnings("ignore")

logger = logging.getLogger(__name__)


@dataclass
class SystemRequirements:
    """Hardware and runtime requirements for MICA-Lineage."""

    min_python_version: Tuple[int, int] = (3, 8)
    recommended_python_version: Tuple[int, int] = (3, 10)
    min_ram_gb: int = 16
    recommended_ram_gb: int = 32
    min_gpu_memory_gb: int = 8
    recommended_gpu_memory_gb: int = 24
    min_disk_space_gb: int = 50
    recommended_disk_space_gb: int = 100


@dataclass
class CompatibilityResult:
    """Represents the outcome of a single compatibility check."""

    component: str
    status: str  # "ok", "warning", "error", "missing"
    version_found: Optional[str] = None
    version_required: Optional[str] = None
    details: Optional[str] = None
    action_required: Optional[str] = None


class MICALineageCompatibilityChecker:
    """End-to-end compatibility validator for the MICA-Lineage platform."""

    def __init__(self) -> None:
        self.system_reqs = SystemRequirements()
        self.results: List[CompatibilityResult] = []

        # Some packages expose modules with different import names.
        self.import_overrides: Dict[str, str] = {
            "fair-esm": "esm",
            "faiss-cpu": "faiss",
            "pymol-py": "pymol",
        }

        # Core packages with version requirements (Phase 4.1 deliverable).
        self.core_packages: Dict[str, str] = {
            "torch": ">=2.0.0",
            "transformers": ">=4.30.0",
            "fair-esm": ">=2.0.0",
            "numpy": ">=1.21.0",
            "scipy": ">=1.7.0",
            "pandas": ">=1.3.0",
            "scikit-learn": ">=1.0.0",
            "pymilvus": ">=2.3.0",
            "neo4j": ">=5.0.0",
            "fastapi": ">=0.68.0",
            "pydantic": ">=2.0.0",
            "psutil": ">=5.9.0",
            "packaging": ">=23.1",
        }

        # Bioinformatics packages (multi-modal embedding + MD stack).
        self.bio_packages: Dict[str, str] = {
            "biopython": ">=1.79",
            "mdanalysis": ">=2.0.0",
            "biotite": ">=0.30.0",
            "rdkit": ">=2022.03.0",
            "prody": ">=2.0.0",
        }

        # Performance optimisation packages.
        self.performance_packages: Dict[str, str] = {
            "numba": ">=0.56.0",
            "orjson": ">=3.8.0",
            "torch-geometric": ">=2.3.0",
            "faiss-cpu": ">=1.7.0",
        }

        # Optional advanced packages (helpful but not mandatory in all setups).
        self.advanced_packages: Dict[str, str] = {
            "esm": ">=2.0.0",
            "colabfold": ">=1.5.0",
            "openmm": ">=7.7.0",
            "mdtraj": ">=1.9.0",
            "pymol-py": ">=2.5.0",
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def check_system_compatibility(self) -> Dict[str, Any]:
        """Run the full compatibility assessment and return the report data."""

        self.results.clear()
        logger.info("Starting MICA-Lineage system compatibility assessment")

        print("🔍 MICA-Lineage System Compatibility Check")
        print("=" * 60)

        self._check_python_version()
        self._check_system_resources()
        self._check_cuda_availability()

        self._check_package_group("Core ML/AI Packages", self.core_packages)
        self._check_package_group("Bioinformatics Packages", self.bio_packages)
        self._check_package_group("Performance Optimisation", self.performance_packages)
        self._check_package_group("Advanced Features (Optional)", self.advanced_packages)

        self._check_model_availability()
        return self._generate_compatibility_report()

    def save_report(self, output_path: str | Path) -> Path:
        """Persist the latest compatibility report as JSON."""

        path = Path(output_path)
        report = self._generate_compatibility_report()
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\n📄 Report saved to: {path}")
        return path

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _check_python_version(self) -> None:
        current_version = sys.version_info[:2]
        found = f"{current_version[0]}.{current_version[1]}"

        if current_version < self.system_reqs.min_python_version:
            self.results.append(
                CompatibilityResult(
                    component="Python Version",
                    status="error",
                    version_found=found,
                    version_required=f">={self.system_reqs.min_python_version[0]}.{self.system_reqs.min_python_version[1]}",
                    details=f"Python {found} is too old",
                    action_required="Update to Python 3.8+ (recommended: 3.10+)",
                )
            )
            return

        if current_version < self.system_reqs.recommended_python_version:
            self.results.append(
                CompatibilityResult(
                    component="Python Version",
                    status="warning",
                    version_found=found,
                    version_required=f">={self.system_reqs.recommended_python_version[0]}.{self.system_reqs.recommended_python_version[1]} (recommended)",
                    details=f"Python {found} works but upgrading is recommended",
                )
            )
            return

        self.results.append(
            CompatibilityResult(
                component="Python Version",
                status="ok",
                version_found=found,
                details="Python version is compatible",
            )
        )

    def _check_system_resources(self) -> None:
        if psutil is None:
            self.results.append(
                CompatibilityResult(
                    component="System Resources",
                    status="warning",
                    details="psutil not installed; skipping RAM and disk checks",
                    action_required="Install psutil>=5.9.0 to enable resource validation",
                )
            )
            return

        total_ram_gb = psutil.virtual_memory().total / (1024 ** 3)
        free_disk_gb = psutil.disk_usage(Path.cwd().anchor).free / (1024 ** 3)

        if total_ram_gb < self.system_reqs.min_ram_gb:
            self.results.append(
                CompatibilityResult(
                    component="System RAM",
                    status="error",
                    version_found=f"{total_ram_gb:.1f} GB",
                    version_required=f">={self.system_reqs.min_ram_gb} GB",
                    details="Insufficient RAM for large models",
                    action_required="Upgrade RAM or use smaller models",
                )
            )
        elif total_ram_gb < self.system_reqs.recommended_ram_gb:
            self.results.append(
                CompatibilityResult(
                    component="System RAM",
                    status="warning",
                    version_found=f"{total_ram_gb:.1f} GB",
                    version_required=f">={self.system_reqs.recommended_ram_gb} GB (recommended)",
                    details="RAM sufficient for baseline workloads",
                )
            )
        else:
            self.results.append(
                CompatibilityResult(
                    component="System RAM",
                    status="ok",
                    version_found=f"{total_ram_gb:.1f} GB",
                    details="Sufficient RAM for all operations",
                )
            )

        if free_disk_gb < self.system_reqs.min_disk_space_gb:
            self.results.append(
                CompatibilityResult(
                    component="Disk Space",
                    status="warning",
                    version_found=f"{free_disk_gb:.1f} GB free",
                    version_required=f">={self.system_reqs.min_disk_space_gb} GB",
                    details="Limited disk space for models and datasets",
                )
            )
        else:
            self.results.append(
                CompatibilityResult(
                    component="Disk Space",
                    status="ok",
                    version_found=f"{free_disk_gb:.1f} GB free",
                    details="Sufficient disk space",
                )
            )

    def _check_cuda_availability(self) -> None:
        if torch is None:
            self.results.append(
                CompatibilityResult(
                    component="CUDA/GPU",
                    status="warning",
                    details="PyTorch not installed; unable to validate CUDA",
                    action_required="Install torch>=2.0.0 with the desired CUDA runtime",
                )
            )
            return

        if not torch.cuda.is_available():
            self.results.append(
                CompatibilityResult(
                    component="CUDA/GPU",
                    status="warning",
                    details="No CUDA GPU detected - CPU execution will be slow",
                    action_required="Install CUDA drivers or use smaller models",
                )
            )
            return

        gpu_count = torch.cuda.device_count()
        gpu_descriptions = []
        for idx in range(gpu_count):
            props = torch.cuda.get_device_properties(idx)
            gpu_descriptions.append(f"GPU {idx}: {props.name} ({props.total_memory / (1024 ** 3):.1f} GB)")

        primary_memory = torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
        if primary_memory < self.system_reqs.min_gpu_memory_gb:
            self.results.append(
                CompatibilityResult(
                    component="GPU Memory",
                    status="warning",
                    version_found=f"{primary_memory:.1f} GB",
                    version_required=f">={self.system_reqs.min_gpu_memory_gb} GB",
                    details="; ".join(gpu_descriptions),
                    action_required="Use smaller batch sizes or upgrade GPU memory",
                )
            )
        else:
            self.results.append(
                CompatibilityResult(
                    component="GPU Memory",
                    status="ok",
                    version_found=f"{primary_memory:.1f} GB",
                    details="; ".join(gpu_descriptions),
                )
            )

        cuda_version = getattr(torch.version, "cuda", "unknown")
        self.results.append(
            CompatibilityResult(
                component="CUDA Version",
                status="ok" if cuda_version != "unknown" else "warning",
                version_found=cuda_version,
                details=f"CUDA {cuda_version} detected" if cuda_version != "unknown" else "CUDA version not reported",
            )
        )

    def _check_package_group(self, group_name: str, packages: Dict[str, str]) -> None:
        print(f"\n📦 Checking {group_name}...")

        for package_name, requirement in packages.items():
            canonical_name = package_name
            module_name = self.import_overrides.get(package_name, package_name.replace("-", "_"))

            try:
                module = importlib.import_module(module_name)
                version = getattr(module, "__version__", "unknown")
            except Exception:
                self.results.append(
                    CompatibilityResult(
                        component=canonical_name,
                        status="missing",
                        version_required=requirement,
                        details=f"❌ {canonical_name} not installed",
                        action_required=f"Install {canonical_name}{requirement}",
                    )
                )
                print(f"  ❌ {canonical_name} not installed (requirement: {requirement})")
                continue

            if version == "unknown":
                self.results.append(
                    CompatibilityResult(
                        component=canonical_name,
                        status="warning",
                        version_found=version,
                        version_required=requirement,
                        details=f"⚠ Unable to determine version for {canonical_name}",
                        action_required=f"Verify {canonical_name} meets {requirement}",
                    )
                )
                print(f"  ⚠ {canonical_name} version unknown (requirement: {requirement})")
                continue

            if self._version_satisfies(version, requirement):
                self.results.append(
                    CompatibilityResult(
                        component=canonical_name,
                        status="ok",
                        version_found=version,
                        version_required=requirement,
                        details=f"✓ {canonical_name} {version}",
                    )
                )
                print(f"  ✓ {canonical_name} {version}")
            else:
                self.results.append(
                    CompatibilityResult(
                        component=canonical_name,
                        status="warning",
                        version_found=version,
                        version_required=requirement,
                        details=f"⚠ {canonical_name} {version} (requirement: {requirement})",
                        action_required=f"Update {canonical_name} to meet {requirement}",
                    )
                )
                print(f"  ⚠ {canonical_name} {version} (requirement: {requirement})")

    def _check_model_availability(self) -> None:
        print("\n🤖 Checking Model Availability...")
        try:
            from transformers import AutoTokenizer  # type: ignore

            AutoTokenizer.from_pretrained("facebook/esm2_t30_150M_UR50D")
            self.results.append(
                CompatibilityResult(
                    component="ESM Models Access",
                    status="ok",
                    details="✓ Hugging Face access confirmed for ESM models",
                )
            )
            print("  ✓ ESM models accessible via Hugging Face")
        except Exception as exc:
            self.results.append(
                CompatibilityResult(
                    component="ESM Models Access",
                    status="warning",
                    details=f"⚠ Unable to access ESM models: {str(exc)[:120]}...",
                    action_required="Verify internet connectivity and Hugging Face credentials",
                )
            )
            print(f"  ⚠ ESM model access issue: {str(exc)[:120]}...")

        try:
            import esm  # type: ignore

            _ = getattr(esm, "__version__", "unknown")
            self.results.append(
                CompatibilityResult(
                    component="fair-esm Models",
                    status="ok",
                    details="✓ fair-esm library available",
                )
            )
            print("  ✓ fair-esm available")
        except Exception:
            self.results.append(
                CompatibilityResult(
                    component="fair-esm Models",
                    status="warning",
                    details="⚠ fair-esm not available (ESM-C 6B models may be unavailable)",
                    action_required="Install fair-esm>=2.0.0",
                )
            )
            print("  ⚠ fair-esm not available")

    def _version_satisfies(self, installed: str, spec: str) -> bool:
        try:
            parsed_version = Version(installed)
        except Exception:
            return False

        try:
            requirement = SpecifierSet(spec)
        except Exception:
            return True

        return parsed_version in requirement

    def _generate_compatibility_report(self) -> Dict[str, Any]:
        status_counts = {
            "ok": sum(1 for r in self.results if r.status == "ok"),
            "warning": sum(1 for r in self.results if r.status == "warning"),
            "error": sum(1 for r in self.results if r.status == "error"),
            "missing": sum(1 for r in self.results if r.status == "missing"),
        }

        if status_counts["error"] > 0:
            overall_status = "❌ CRITICAL ISSUES FOUND"
        elif status_counts["missing"] > 0 or status_counts["warning"] > 0:
            overall_status = "⚠ ISSUES FOUND (system may work with limitations)"
        else:
            overall_status = "✅ ALL CHECKS PASSED"

        action_items = [
            f"• {result.component}: {result.action_required}"
            for result in self.results
            if result.action_required
        ]

        report = {
            "overall_status": overall_status,
            "status_counts": status_counts,
            "system_info": {
                "platform": platform.platform(),
                "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                "torch_version": getattr(torch, "__version__", "Not installed"),
                "cuda_available": bool(torch and torch.cuda.is_available()),
            },
            "detailed_results": [
                {
                    "component": result.component,
                    "status": result.status,
                    "version_found": result.version_found,
                    "version_required": result.version_required,
                    "details": result.details,
                    "action_required": result.action_required,
                }
                for result in self.results
            ],
            "action_items": action_items,
            "references": [
                "requirements_mica_lineage_core.txt",
                "requirements_mica_lineage_complete.txt",
            ],
        }

        print(f"\n{'=' * 60}")
        print("🎯 COMPATIBILITY REPORT SUMMARY")
        print("=" * 60)
        print(f"Overall Status: {overall_status}")
        print(f"✅ Passed: {status_counts['ok']}")
        print(f"⚠ Warnings: {status_counts['warning']}")
        print(f"❌ Errors: {status_counts['error']}")
        print(f"📦 Missing: {status_counts['missing']}")

        if action_items:
            print("\n🔧 ACTION ITEMS:")
            for item in action_items:
                print(f"  {item}")

        print("\n💡 For detailed installation instructions, see:")
        print("   • requirements_mica_lineage_core.txt")
        print("   • requirements_mica_lineage_complete.txt")

        return report


def _parse_args(args: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MICA-Lineage compatibility checker")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("mica_lineage_compatibility_report.json"),
        help="Path to store the JSON report (defaults to current directory).",
    )
    return parser.parse_args(args=args)


def main(cli_args: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """Command-line entry point."""

    options = _parse_args(cli_args)
    checker = MICALineageCompatibilityChecker()
    report = checker.check_system_compatibility()
    checker.save_report(options.output)
    return report


if __name__ == "__main__":  # pragma: no cover - CLI invocation
    main()
