"""
MICA Native MCP Servers (In-Process / Zero Latency)

Native MCP servers that run in-process using FastMCP In-Memory Client.
These provide MCP metadata and trazabilidad without subprocess overhead.

Available Servers:
- rdkit_native_mcp: RDKit cheminformatics (12 tools)
- dlm_native_mcp: DLM literature & biological context (6 tools)

Usage:
    from mica.mcp_servers.rdkit_native_mcp import rdkit_native_server
    from mica.mcp_servers.dlm_native_mcp import dlm_native_server
    from fastmcp import Client
    
    client = Client(rdkit_native_server)  # In-process, zero latency
    result = await client.call_tool("calculate_molecular_weight", {"smiles": "CC(=O)O"})
    
    dlm_client = Client(dlm_native_server)
    result = await dlm_client.call_tool("dlm_get_biological_context", {"gene_name": "WNK1"})
"""

from .rdkit_native_mcp import rdkit_native_server
from .dlm_native_mcp import dlm_native_server

__all__ = ["rdkit_native_server", "dlm_native_server"]
