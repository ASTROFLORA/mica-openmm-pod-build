"""
BSM Dynamo Module
=================

drMD recipe integration for MICA Dynamo worker.

Provides Pydantic schemas for YAML-based MD simulation recipes with:
- Multi-stage protocols (minimize, equilibrate, production)
- Physical quantity validation
- Conversion to Dynamo API parameters
"""

from .recipes import (
    DynamoRecipe,
    SimulationStage,
    PathInfo,
    HardwareInfo,
    TemperatureControl,
    PressureControl,
    ForceField,
    WaterModel,
    EnsembleType,
    IntegratorType,
    SchemaVersion,
    create_standard_protein_recipe,
)

__all__ = [
    "DynamoRecipe",
    "SimulationStage",
    "PathInfo",
    "HardwareInfo",
    "TemperatureControl",
    "PressureControl",
    "ForceField",
    "WaterModel",
    "EnsembleType",
    "IntegratorType",
    "SchemaVersion",
    "create_standard_protein_recipe",
]
