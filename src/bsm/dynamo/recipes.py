"""
drMD Recipe Schemas for MICA Dynamo Integration
================================================

Pydantic models for drMD YAML recipe validation and conversion to Dynamo parameters.

Implements best practices from Perplexity research:
- Field-level validation with constraints
- Model validators for cross-field validation
- Physical quantity type safety
- Custom error messages
- Schema versioning

References:
- drMD ExampleInputs/*.yaml
- drMD Triage/drConfigTriage.py validation logic
- yaml_recipe_integration.md implementation plan
- OpenMM unit system integration
"""

from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator


class SchemaVersion(str, Enum):
    """
    Schema version for backward compatibility
    """
    V1_0 = "1.0"
    V1_1 = "1.1"


class ForceField(str, Enum):
    """
    Supported force fields
    """
    AMBER14 = "amber14-all.xml"
    AMBER99SB = "amber99sb.xml"
    CHARMM36 = "charmm36.xml"
    AMBER_TIP3P = "amber14/tip3pfb.xml"


class WaterModel(str, Enum):
    """
    Water models for solvation
    """
    TIP3P = "tip3p"
    TIP4P = "tip4p"
    TIP5P = "tip5p"
    SPCE = "spce"


class EnsembleType(str, Enum):
    """
    Thermodynamic ensembles
    """
    NVE = "NVE"  # Microcanonical
    NVT = "NVT"  # Canonical
    NPT = "NPT"  # Isothermal-isobaric


class IntegratorType(str, Enum):
    """
    MD integrators
    """
    LANGEVIN = "LangevinMiddleIntegrator"
    VERLET = "VerletIntegrator"
    BROWNIAN = "BrownianIntegrator"
    NOSE_HOOVER = "NoseHooverIntegrator"


class PathInfo(BaseModel):
    """
    File paths for input/output
    
    Validation ensures paths exist or parent directories are writable
    """
    input_pdb: Path = Field(..., description="Input PDB structure file")
    output_dir: Path = Field(..., description="Output directory for trajectories")
    log_dir: Optional[Path] = Field(None, description="Directory for log files")
    
    @field_validator('input_pdb')
    @classmethod
    def validate_input_exists(cls, v: Path) -> Path:
        """Ensure input PDB exists"""
        if not v.exists():
            raise ValueError(f"Input PDB file not found: {v}")
        if v.suffix.lower() not in ['.pdb', '.pdb.gz']:
            raise ValueError(f"Input must be PDB file: {v}")
        return v
    
    @field_validator('output_dir')
    @classmethod
    def ensure_output_writable(cls, v: Path) -> Path:
        """Ensure output directory exists or can be created"""
        v.mkdir(parents=True, exist_ok=True)
        if not v.is_dir():
            raise ValueError(f"Output path is not a directory: {v}")
        return v


class HardwareInfo(BaseModel):
    """
    Hardware configuration for simulation
    
    Implements CPU throttling from drMD and GPU provisioning from SuperDynamo
    """
    platform: str = Field(default="CUDA", description="OpenMM platform (CUDA, OpenCL, CPU)")
    device_index: Optional[int] = Field(None, description="GPU device index")
    cpu_threads: Optional[int] = Field(None, description="Number of CPU threads")
    precision: str = Field(default="mixed", description="Precision (single, mixed, double)")
    enable_mps: bool = Field(default=False, description="Enable NVIDIA MPS for multi-tenancy")
    
    @field_validator('platform')
    @classmethod
    def validate_platform(cls, v: str) -> str:
        """Validate platform choice"""
        valid_platforms = {'CUDA', 'OpenCL', 'CPU', 'Reference'}
        if v not in valid_platforms:
            raise ValueError(f"Platform must be one of {valid_platforms}, got {v}")
        return v
    
    @field_validator('cpu_threads')
    @classmethod
    def validate_cpu_threads(cls, v: Optional[int]) -> Optional[int]:
        """Ensure CPU thread count is reasonable"""
        if v is not None:
            if v < 1:
                raise ValueError("CPU threads must be >= 1")
            if v > 256:
                raise ValueError("CPU threads > 256 is excessive; check configuration")
        return v
    
    @model_validator(mode='after')
    def validate_gpu_config(self):
        """Ensure GPU config is consistent"""
        if self.platform == 'CUDA' and self.device_index is None:
            # Auto-select device 0
            self.device_index = 0
        return self


class TemperatureControl(BaseModel):
    """
    Temperature control parameters with unit validation
    
    Inspired by pydantic-units for physical quantities
    """
    target_temperature: float = Field(..., gt=0, le=1000, description="Target temperature in Kelvin")
    friction_coefficient: Optional[float] = Field(
        None, 
        gt=0, 
        description="Friction coefficient for Langevin (1/ps)"
    )
    
    @field_validator('target_temperature')
    @classmethod
    def validate_temperature_physical(cls, v: float) -> float:
        """Ensure temperature is physically reasonable"""
        if v < 100:
            raise ValueError(
                f"Temperature {v} K is very low; typically > 100 K for biomolecular MD. "
                "Check units (Kelvin expected)."
            )
        if v > 500:
            raise ValueError(
                f"Temperature {v} K is very high for typical biomolecular simulations. "
                "Check for typo (e.g., 300 K not 3000 K)."
            )
        return v


class PressureControl(BaseModel):
    """
    Pressure control for NPT ensemble
    """
    target_pressure: float = Field(default=1.0, gt=0, description="Target pressure in bar")
    barostat_frequency: int = Field(default=25, gt=0, description="Barostat update frequency (steps)")
    
    @field_validator('target_pressure')
    @classmethod
    def validate_pressure_range(cls, v: float) -> float:
        """Ensure pressure is physically reasonable"""
        if v < 0.1 or v > 1000:
            raise ValueError(
                f"Pressure {v} bar outside typical range (0.1-1000 bar). "
                "Check units (bar expected)."
            )
        return v


class SimulationStage(BaseModel):
    """
    Single simulation stage (e.g., minimization, equilibration, production)
    
    Implements drMD-style multi-stage protocols
    """
    stage_name: str = Field(..., description="Human-readable stage name")
    stage_type: str = Field(..., description="Stage type (minimize, equilibrate, production)")
    ensemble: Optional[EnsembleType] = Field(None, description="Thermodynamic ensemble")
    integrator: IntegratorType = Field(default=IntegratorType.LANGEVIN, description="MD integrator")
    timestep: float = Field(default=2.0, gt=0, le=5.0, description="Timestep in femtoseconds")
    duration: Optional[float] = Field(None, gt=0, description="Simulation duration (ps for MD, steps for minimize)")
    temperature_control: Optional[TemperatureControl] = Field(None, description="Temperature control")
    pressure_control: Optional[PressureControl] = Field(None, description="Pressure control for NPT")
    restraints: Optional[Dict[str, Any]] = Field(None, description="Position restraints")
    save_frequency: int = Field(default=5000, gt=0, description="Frames to save per output")
    
    @field_validator('stage_type')
    @classmethod
    def validate_stage_type(cls, v: str) -> str:
        """Ensure stage type is recognized"""
        valid_types = {'minimize', 'equilibrate', 'production', 'metadynamics'}
        if v not in valid_types:
            raise ValueError(f"Stage type must be one of {valid_types}, got {v}")
        return v
    
    @field_validator('timestep')
    @classmethod
    def validate_timestep_stability(cls, v: float) -> float:
        """Warn if timestep may be unstable"""
        if v > 4.0:
            raise ValueError(
                f"Timestep {v} fs is large; may cause instability without SHAKE/SETTLE. "
                "Consider 2 fs with constraints or 1 fs without."
            )
        return v
    
    @model_validator(mode='after')
    def validate_ensemble_consistency(self):
        """Ensure ensemble and controls are consistent"""
        if self.ensemble == EnsembleType.NPT and not self.pressure_control:
            raise ValueError("NPT ensemble requires pressure_control parameters")
        
        if self.ensemble in [EnsembleType.NVT, EnsembleType.NPT] and not self.temperature_control:
            raise ValueError(f"{self.ensemble} ensemble requires temperature_control parameters")
        
        # Ensure duration is specified for dynamics stages
        if self.stage_type in {'equilibrate', 'production', 'metadynamics'} and not self.duration:
            raise ValueError(f"Stage type {self.stage_type} requires duration specification")
        
        return self


class DynamoRecipe(BaseModel):
    """
    Complete drMD-style recipe for MICA Dynamo execution
    
    Top-level model with cross-stage validation
    """
    schema_version: SchemaVersion = Field(default=SchemaVersion.V1_0, description="Schema version")
    recipe_name: str = Field(..., description="Descriptive recipe name")
    paths: PathInfo = Field(..., description="Input/output paths")
    hardware: HardwareInfo = Field(default_factory=HardwareInfo, description="Hardware configuration")
    force_field: ForceField = Field(default=ForceField.AMBER14, description="Force field selection")
    water_model: Optional[WaterModel] = Field(None, description="Water model for solvation")
    stages: List[SimulationStage] = Field(..., min_length=1, description="Simulation stages")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional metadata")
    
    @model_validator(mode='after')
    def validate_stage_sequence(self):
        """
        Validate that stage sequence makes sense
        
        Best practice: minimize → equilibrate → production
        """
        stage_types = [s.stage_type for s in self.stages]
        
        # Check for minimization before dynamics
        if 'production' in stage_types and 'minimize' not in stage_types:
            raise ValueError(
                "Production runs should be preceded by energy minimization. "
                "Add a 'minimize' stage."
            )
        
        # Check for equilibration before production
        if 'production' in stage_types and 'equilibrate' not in stage_types:
            # This is a warning, not an error, but we can't emit warnings in validators easily
            # Could log or add to metadata
            self.metadata['warnings'] = self.metadata.get('warnings', [])
            self.metadata['warnings'].append(
                "Production without equilibration: consider adding equilibration stage"
            )
        
        return self
    
    @classmethod
    def from_yaml(cls, yaml_path: Path) -> 'DynamoRecipe':
        """
        Load recipe from YAML file
        
        Implements two-stage parsing: PyYAML → Pydantic validation
        """
        import yaml
        
        with open(yaml_path, 'r') as f:
            data = yaml.safe_load(f)
        
        return cls(**data)
    
    def to_dynamo_params(self) -> Dict[str, Any]:
        """
        Convert recipe to Dynamo worker parameters
        
        Maps drMD recipe concepts to SuperDynamo/BioDynamo API
        """
        # Extract first production stage for main simulation
        production_stages = [s for s in self.stages if s.stage_type == 'production']
        if not production_stages:
            raise ValueError("Recipe must have at least one production stage")
        
        main_stage = production_stages[0]
        
        # Build Dynamo parameters
        params = {
            'simulation_type': main_stage.ensemble.value if main_stage.ensemble else 'NVT',
            'input_pdb': str(self.paths.input_pdb),
            'output_dir': str(self.paths.output_dir),
            'n_atoms': None,  # Will be read from PDB
            'timestep': main_stage.timestep,
            'duration_ps': main_stage.duration,
            'platform': self.hardware.platform,
            'device_index': self.hardware.device_index,
            'precision': self.hardware.precision,
            'force_field': self.force_field.value,
        }
        
        # Add temperature control
        if main_stage.temperature_control:
            params['temperature'] = main_stage.temperature_control.target_temperature
            if main_stage.temperature_control.friction_coefficient:
                params['friction'] = main_stage.temperature_control.friction_coefficient
        
        # Add pressure control for NPT
        if main_stage.pressure_control:
            params['pressure'] = main_stage.pressure_control.target_pressure
            params['barostat_frequency'] = main_stage.pressure_control.barostat_frequency
        
        # Add metadata
        params['recipe_name'] = self.recipe_name
        params['recipe_metadata'] = self.metadata
        
        # Flag multi-stage execution if needed
        if len(self.stages) > 1:
            params['multi_stage'] = True
            params['all_stages'] = [s.model_dump() for s in self.stages]
        
        return params
    
    class Config:
        json_schema_extra = {
            "example": {
                "schema_version": "1.0",
                "recipe_name": "Standard_Protein_MD_300K",
                "paths": {
                    "input_pdb": "/data/structures/protein.pdb",
                    "output_dir": "/data/outputs/protein_md",
                    "log_dir": "/data/logs"
                },
                "hardware": {
                    "platform": "CUDA",
                    "device_index": 0,
                    "precision": "mixed"
                },
                "force_field": "amber14-all.xml",
                "water_model": "tip3p",
                "stages": [
                    {
                        "stage_name": "Energy Minimization",
                        "stage_type": "minimize",
                        "integrator": "LangevinMiddleIntegrator",
                        "duration": 1000,
                        "save_frequency": 100
                    },
                    {
                        "stage_name": "NVT Equilibration",
                        "stage_type": "equilibrate",
                        "ensemble": "NVT",
                        "integrator": "LangevinMiddleIntegrator",
                        "timestep": 2.0,
                        "duration": 100,
                        "temperature_control": {
                            "target_temperature": 300.0,
                            "friction_coefficient": 1.0
                        },
                        "save_frequency": 1000
                    },
                    {
                        "stage_name": "NPT Production",
                        "stage_type": "production",
                        "ensemble": "NPT",
                        "integrator": "LangevinMiddleIntegrator",
                        "timestep": 2.0,
                        "duration": 10000,
                        "temperature_control": {
                            "target_temperature": 300.0,
                            "friction_coefficient": 1.0
                        },
                        "pressure_control": {
                            "target_pressure": 1.0,
                            "barostat_frequency": 25
                        },
                        "save_frequency": 5000
                    }
                ]
            }
        }


# Helper functions for common recipes

def create_standard_protein_recipe(
    input_pdb: Path,
    output_dir: Path,
    temperature: float = 300.0,
    duration_ps: float = 10000.0,
    gpu_index: int = 0
) -> DynamoRecipe:
    """
    Create standard 3-stage protein MD recipe
    
    Stages: minimize → NVT equilibration → NPT production
    """
    return DynamoRecipe(
        recipe_name=f"Standard_Protein_{temperature}K_{duration_ps}ps",
        paths=PathInfo(input_pdb=input_pdb, output_dir=output_dir),
        hardware=HardwareInfo(platform="CUDA", device_index=gpu_index),
        force_field=ForceField.AMBER14,
        water_model=WaterModel.TIP3P,
        stages=[
            SimulationStage(
                stage_name="Energy Minimization",
                stage_type="minimize",
                integrator=IntegratorType.LANGEVIN,
                timestep=1.0,
                duration=1000,
                save_frequency=100
            ),
            SimulationStage(
                stage_name="NVT Equilibration",
                stage_type="equilibrate",
                ensemble=EnsembleType.NVT,
                integrator=IntegratorType.LANGEVIN,
                timestep=2.0,
                duration=100,
                temperature_control=TemperatureControl(
                    target_temperature=temperature,
                    friction_coefficient=1.0
                ),
                save_frequency=1000
            ),
            SimulationStage(
                stage_name="NPT Production",
                stage_type="production",
                ensemble=EnsembleType.NPT,
                integrator=IntegratorType.LANGEVIN,
                timestep=2.0,
                duration=duration_ps,
                temperature_control=TemperatureControl(
                    target_temperature=temperature,
                    friction_coefficient=1.0
                ),
                pressure_control=PressureControl(
                    target_pressure=1.0,
                    barostat_frequency=25
                ),
                save_frequency=5000
            )
        ]
    )
