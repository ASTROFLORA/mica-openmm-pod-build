# 🏗️ MICA MCP Server Creation Standard

**Version:** 1.0.0  
**Last Updated:** December 15, 2025  
**Status:** Production Standard

---

## 📋 Table of Contents

1. [Overview](#overview)
2. [Server Types](#server-types)
3. [Standard Architecture](#standard-architecture)
4. [IN_PROCESS Server Template](#in_process-server-template)
5. [Subprocess Server Template](#subprocess-server-template)
6. [Required Components](#required-components)
7. [Best Practices](#best-practices)
8. [Testing Requirements](#testing-requirements)
9. [Documentation Requirements](#documentation-requirements)
10. [Examples](#examples)

---

## 📖 Overview

This document defines the **official standard** for creating Model Context Protocol (MCP) servers in the MICA platform. All new MCP servers **MUST** follow these guidelines to ensure consistency, reliability, and maintainability.

### Goals

- ✅ **Consistency**: All servers follow the same patterns
- ✅ **Reliability**: Comprehensive error handling and validation
- ✅ **Observability**: Structured logging and metrics
- ✅ **Security**: Rate limiting and input validation
- ✅ **Maintainability**: Clear documentation and versioning

---

## 🔧 Server Types

### 1. IN_PROCESS Servers (FastMCP)

**Characteristics:**
- Execute in the same Python process as MICA
- Zero serialization overhead (<1ms latency)
- Direct Python function calls
- Shared memory space
- Best for: Computationally intensive operations

**Examples:**
- RDKit Native MCP (cheminformatics)
- NumPy/SciPy operations
- ML model inference (local)

**Pros:**
- ⚡ Ultra-low latency (<1ms)
- 🎯 Direct memory access
- 🔄 No subprocess overhead

**Cons:**
- ⚠️ Shares memory with main process
- ⚠️ Crash affects main process
- ⚠️ Limited to Python libraries

---

### 2. Subprocess Servers (stdio/JSON-RPC)

**Characteristics:**
- Execute as separate processes
- Communication via stdio (JSON-RPC)
- Process isolation
- Language-agnostic
- Best for: External APIs, multi-language tools

**Examples:**
- AlphaFold MCP (API calls)
- UniProt MCP (REST API)
- PubMed MCP (NCBI E-utilities)

**Pros:**
- 🛡️ Process isolation
- 🌐 Language-agnostic
- 🔒 Fault isolation

**Cons:**
- 🐌 Higher latency (100-500ms)
- 📦 Serialization overhead
- 🔄 Subprocess management

---

## 🏛️ Standard Architecture

### Required File Structure

```
src/mica/mcp_servers/
├── your_server_mcp.py          # Server implementation
├── docs/
│   ├── YOUR_SERVER_MCP.md      # Server documentation
│   └── YOUR_SERVER_GUIDE.md    # Usage guide
└── tests/
    └── test_your_server.py     # Test suite
```

### Configuration Entry

**File:** `src/mica/config/mcp_servers.json`

```json
{
  "your_server": {
    "command": "IN_PROCESS",  // or "python"
    "args": ["mica.mcp_servers.your_server_mcp:your_server"],
    "mode": "in_process",  // or "subprocess"
    "tools_count": 25,
    "priority": "GOLD-1",
    "description": "Brief description of server capabilities",
    "category": "bioinformatics"  // or "chemistry", "ml", etc.
  }
}
```

---

## 🚀 IN_PROCESS Server Template

### Minimal Template

```python
"""
🧬 Your Server Name - IN_PROCESS MCP Server

DESCRIPTION:
Brief description of what this server does.

FEATURES:
- Feature 1
- Feature 2
- Feature 3

ARCHITECTURE:
- FastMCP In-Memory Server (NO subprocess)
- Zero latency communication
- Direct Python function calls
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import datetime
from functools import wraps
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

from fastmcp import FastMCP
from pydantic import BaseModel, Field, field_validator

# Import your core library
try:
    import your_library
    LIBRARY_AVAILABLE = True
    LIBRARY_VERSION = your_library.__version__
except ImportError:
    LIBRARY_AVAILABLE = False
    LIBRARY_VERSION = "unknown"
    logging.warning("Your library not available")

# Setup logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Server metadata
SERVER_VERSION = "1.0.0"
SERVER_NAME = "Your-Server-Name"

# ============================================================================
# CUSTOM EXCEPTIONS
# ============================================================================

class YourServerError(Exception):
    """Base exception for your server."""
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

class ValidationError(YourServerError):
    """Input validation failed."""
    pass

class ComputationError(YourServerError):
    """Computation failed."""
    pass

class RateLimitError(YourServerError):
    """Rate limit exceeded."""
    pass

# ============================================================================
# PYDANTIC MODELS FOR VALIDATION
# ============================================================================

class InputParams(BaseModel):
    """Base input parameters."""
    data: str = Field(..., min_length=1, max_length=10000, description="Input data")
    
    @field_validator('data')
    @classmethod
    def validate_data(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Data cannot be empty")
        return v.strip()

# Add more specific parameter models as needed

# ============================================================================
# RATE LIMITER
# ============================================================================

class RateLimiter:
    """Token bucket rate limiter."""
    
    def __init__(self, max_calls: int = 100, window_seconds: int = 60):
        self.max_calls = max_calls
        self.window = window_seconds
        self.calls: Dict[str, List[float]] = defaultdict(list)
        self.lock = Lock()
    
    def allow_request(self, client_id: str = "default") -> Tuple[bool, str]:
        with self.lock:
            now = time.time()
            self.calls[client_id] = [
                t for t in self.calls[client_id]
                if now - t < self.window
            ]
            
            if len(self.calls[client_id]) >= self.max_calls:
                return False, f"Rate limit exceeded: {self.max_calls} calls per {self.window}s"
            
            self.calls[client_id].append(now)
            return True, ""

# Global rate limiters
rate_limiter = RateLimiter(max_calls=100, window_seconds=60)
heavy_rate_limiter = RateLimiter(max_calls=20, window_seconds=60)

# ============================================================================
# DECORATORS
# ============================================================================

def with_rate_limit(limiter: RateLimiter = rate_limiter):
    """Apply rate limiting to tools."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            allowed, msg = limiter.allow_request()
            if not allowed:
                logger.warning(f"rate_limit_exceeded tool={func.__name__}")
                return {"error": msg, "error_type": "rate_limit_exceeded"}
            return func(*args, **kwargs)
        return wrapper
    return decorator

def with_logging(func):
    """Add structured logging to tools."""
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
            logger.error(f"{tool_name}_failed - error={str(e)}, type={type(e).__name__}, duration_ms={round(duration_ms, 2)}")
            raise
    return wrapper

def with_metadata(func):
    """Add version metadata to tool responses."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        result = func(*args, **kwargs)
        
        if isinstance(result, dict) and "error" not in result:
            result["_metadata"] = {
                "server_version": SERVER_VERSION,
                "library_version": LIBRARY_VERSION,
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "tool_name": func.__name__
            }
        
        return result
    return wrapper

# ============================================================================
# VALIDATION UTILITIES
# ============================================================================

def validate_input(data: str) -> Tuple[bool, str, Optional[Any]]:
    """
    Validate input data.
    
    Returns:
        Tuple of (is_valid, error_message, processed_data)
    """
    if not LIBRARY_AVAILABLE:
        return False, "Library not available", None
    
    if not data or not isinstance(data, str):
        return False, "Data must be a non-empty string", None
    
    data = data.strip()
    
    if len(data) > 10000:
        return False, "Data too long (max 10000 characters)", None
    
    try:
        # Add your specific validation logic here
        processed = data  # Process/parse data
        return True, "", processed
    except Exception as e:
        return False, f"Validation error: {str(e)}", None

# Initialize FastMCP server
your_server = FastMCP(SERVER_NAME)

# ============================================================================
# TOOLS
# ============================================================================

@your_server.tool()  # Read-only, idempotent, non-destructive
@with_rate_limit()
@with_logging
@with_metadata
def example_tool(data: str) -> Dict[str, Any]:
    """
    Example tool description.
    
    Detailed explanation of what this tool does, including:
    - Input requirements
    - Output format
    - Edge cases
    
    Args:
        data: Input data description (max 10000 chars)
        
    Returns:
        Dictionary with:
        - result: computation result
        - status: "success" or "error"
        - _metadata: version and timestamp info
        
    Raises:
        ValidationError: If input data is invalid
        ComputationError: If computation fails
    """
    if not LIBRARY_AVAILABLE:
        return YourServerError("Library not available", "library_unavailable").to_dict()
    
    # Validate input
    is_valid, error_msg, processed_data = validate_input(data)
    if not is_valid:
        raise ValidationError(error_msg, "validation_error")
    
    try:
        # Perform computation
        result = your_library.compute(processed_data)
        
        return {
            "data": data,
            "result": result,
            "status": "success"
        }
    except Exception as e:
        raise ComputationError(f"Computation failed: {str(e)}", "computation_error")

# Add more tools following the same pattern...

# ============================================================================
# EXPORTS
# ============================================================================

__all__ = ["your_server"]

if __name__ == "__main__":
    """Test mode."""
    print(f"🧬 {SERVER_NAME} MCP Server")
    print(f"   Available: {LIBRARY_AVAILABLE}")
    print(f"   Version: {SERVER_VERSION}")
    
    if LIBRARY_AVAILABLE:
        print(f"   Library: {LIBRARY_VERSION}")
```

---

## 🔄 Subprocess Server Template

### Minimal Template

```python
"""
🌐 Your Server Name - Subprocess MCP Server

DESCRIPTION:
Brief description of what this server does.

COMMUNICATION:
- JSON-RPC over stdio
- Process isolation
- External API integration
"""
import json
import logging
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

# Setup logging to stderr (stdout is for MCP communication)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger(__name__)

# Server metadata
SERVER_VERSION = "1.0.0"
SERVER_NAME = "your-server-name"

# ============================================================================
# MCP PROTOCOL HANDLERS
# ============================================================================

class MCPServer:
    """Base MCP server with JSON-RPC protocol."""
    
    def __init__(self, name: str, version: str):
        self.name = name
        self.version = version
        self.tools: Dict[str, callable] = {}
        self.call_count = 0
    
    def register_tool(self, name: str, func: callable, description: str, schema: Dict[str, Any]):
        """Register a tool."""
        self.tools[name] = {
            "func": func,
            "description": description,
            "inputSchema": schema
        }
    
    def handle_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Handle incoming JSON-RPC request."""
        method = request.get("method")
        params = request.get("params", {})
        request_id = request.get("id")
        
        try:
            if method == "initialize":
                return self._handle_initialize(request_id)
            elif method == "tools/list":
                return self._handle_list_tools(request_id)
            elif method == "tools/call":
                return self._handle_call_tool(params, request_id)
            else:
                return self._error_response(request_id, -32601, f"Method not found: {method}")
        except Exception as e:
            logger.error(f"Error handling request: {e}")
            return self._error_response(request_id, -32603, str(e))
    
    def _handle_initialize(self, request_id: int) -> Dict[str, Any]:
        """Handle initialization."""
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {
                    "name": self.name,
                    "version": self.version
                },
                "capabilities": {
                    "tools": {}
                }
            }
        }
    
    def _handle_list_tools(self, request_id: int) -> Dict[str, Any]:
        """List all available tools."""
        tools_list = [
            {
                "name": name,
                "description": tool["description"],
                "inputSchema": tool["inputSchema"]
            }
            for name, tool in self.tools.items()
        ]
        
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "tools": tools_list
            }
        }
    
    def _handle_call_tool(self, params: Dict[str, Any], request_id: int) -> Dict[str, Any]:
        """Execute a tool."""
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        
        if tool_name not in self.tools:
            return self._error_response(request_id, -32602, f"Unknown tool: {tool_name}")
        
        try:
            self.call_count += 1
            start_time = time.time()
            
            result = self.tools[tool_name]["func"](**arguments)
            
            duration_ms = (time.time() - start_time) * 1000
            logger.info(f"Tool {tool_name} executed in {duration_ms:.2f}ms")
            
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(result, indent=2)
                        }
                    ]
                }
            }
        except Exception as e:
            logger.error(f"Tool execution failed: {e}")
            return self._error_response(request_id, -32603, str(e))
    
    def _error_response(self, request_id: int, code: int, message: str) -> Dict[str, Any]:
        """Create error response."""
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": code,
                "message": message
            }
        }
    
    def run(self):
        """Start server main loop."""
        logger.info(f"Starting {self.name} v{self.version}")
        
        for line in sys.stdin:
            try:
                request = json.loads(line)
                response = self.handle_request(request)
                print(json.dumps(response), flush=True)
            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON: {e}")
            except Exception as e:
                logger.error(f"Unexpected error: {e}")

# ============================================================================
# TOOLS IMPLEMENTATION
# ============================================================================

def example_tool(query: str) -> Dict[str, Any]:
    """
    Example tool implementation.
    
    Args:
        query: Input query string
        
    Returns:
        Dictionary with results
    """
    try:
        # Implement your logic here
        result = {
            "query": query,
            "result": f"Processed: {query}",
            "timestamp": datetime.utcnow().isoformat() + "Z"
        }
        
        return result
    except Exception as e:
        return {
            "error": str(e),
            "error_type": "computation_error"
        }

# ============================================================================
# SERVER SETUP
# ============================================================================

def setup_server() -> MCPServer:
    """Initialize and configure the MCP server."""
    server = MCPServer(SERVER_NAME, SERVER_VERSION)
    
    # Register tools
    server.register_tool(
        name="example_tool",
        func=example_tool,
        description="Example tool that processes queries",
        schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Query string to process"
                }
            },
            "required": ["query"]
        }
    )
    
    # Register more tools...
    
    return server

# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    server = setup_server()
    server.run()
```

---

## 📋 Required Components

### 1. Error Handling ⚠️ MANDATORY

All servers MUST implement:

```python
# Custom exception hierarchy
class YourServerError(Exception):
    """Base exception with structured context."""
    def to_dict(self) -> Dict[str, Any]:
        return {
            "error": self.message,
            "error_type": self.error_type,
            "recoverable": self.recoverable,
            "suggestion": self.suggestion
        }

# Specific exceptions
class ValidationError(YourServerError): pass
class ComputationError(YourServerError): pass
class RateLimitError(YourServerError): pass
```

**Why:** Enables automated error handling and debugging.

---

### 2. Input Validation ✅ MANDATORY

All tools MUST validate inputs:

```python
from pydantic import BaseModel, Field, field_validator

class ToolParams(BaseModel):
    """Validated parameters."""
    data: str = Field(..., min_length=1, max_length=10000)
    
    @field_validator('data')
    @classmethod
    def validate_data(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Data cannot be empty")
        return v.strip()
```

**Why:** Prevents crashes, DoS attacks, and bad inputs.

---

### 3. Rate Limiting 🛡️ MANDATORY

All servers MUST implement rate limiting:

```python
# Standard: 100 calls/min
@with_rate_limit()
def normal_tool(...):
    pass

# Heavy: 20 calls/min
@with_rate_limit(heavy_rate_limiter)
def expensive_tool(...):
    pass
```

**Why:** Prevents resource exhaustion and abuse.

---

### 4. Structured Logging 📊 MANDATORY

All tools MUST use logging decorator:

```python
@with_logging
def your_tool(...):
    # Automatic logging:
    # → INFO: your_tool_started - args=2, kwargs=1
    # → INFO: your_tool_completed - duration_ms=12.45, success=True
    pass
```

**Why:** Enables monitoring, debugging, and performance analysis.

---

### 5. Version Metadata 🏷️ MANDATORY

All tools MUST include metadata:

```python
@with_metadata
def your_tool(...):
    # Automatically adds:
    # "_metadata": {
    #     "server_version": "1.0.0",
    #     "library_version": "2025.09.3",
    #     "timestamp": "2025-12-15T18:34:40.067472Z",
    #     "tool_name": "your_tool"
    # }
    pass
```

**Why:** Reproducibility, compliance, and debugging.

---

## 🎯 Best Practices

### 1. Naming Conventions

**Server Files:**
```
✅ GOOD: alphafold_mcp.py, rdkit_native_mcp.py
❌ BAD:  af_server.py, rdkit.py
```

**Tool Names:**
```
✅ GOOD: calculate_molecular_weight, search_protein_by_id
❌ BAD:  calc_mw, search
```

**Variables:**
```python
✅ GOOD: protein_sequence, similarity_threshold
❌ BAD:  seq, thresh, x
```

---

### 2. Documentation

**Tool Docstrings:**
```python
def your_tool(data: str, threshold: float = 0.5) -> Dict[str, Any]:
    """
    Brief one-line description.
    
    Detailed explanation including:
    - What the tool does
    - Use cases
    - Edge cases
    
    Args:
        data: Description (constraints, format, max length)
        threshold: Description (range, default, meaning)
        
    Returns:
        Dictionary with:
        - key1: description
        - key2: description
        - _metadata: version and timestamp info
        
    Raises:
        ValidationError: When input validation fails
        ComputationError: When computation fails
        
    Example:
        >>> result = your_tool("example data", threshold=0.8)
        >>> print(result["key1"])
    """
```

---

### 3. Error Messages

**Good Error Messages:**
```python
✅ GOOD:
{
    "error": "Invalid SMILES 'ABC123': Molecule sanitization failed at atom 3",
    "error_type": "smiles_validation_error",
    "recoverable": false,
    "suggestion": "Check SMILES syntax and ensure valid chemistry"
}

❌ BAD:
{
    "error": "Invalid input"
}
```

---

### 4. Performance

**Rate Limit Tiers:**
```python
# Light operations (< 10ms): 100 calls/min
rate_limiter = RateLimiter(max_calls=100, window_seconds=60)

# Medium operations (10-100ms): 50 calls/min
medium_limiter = RateLimiter(max_calls=50, window_seconds=60)

# Heavy operations (> 100ms): 20 calls/min
heavy_limiter = RateLimiter(max_calls=20, window_seconds=60)

# Very heavy (> 1s): 5 calls/min
very_heavy_limiter = RateLimiter(max_calls=5, window_seconds=60)
```

---

### 5. Security

**Input Sanitization:**
```python
# Maximum lengths
MAX_STRING_LENGTH = 10000
MAX_LIST_LENGTH = 1000
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB

# Validation
def validate_input(data: str) -> bool:
    if len(data) > MAX_STRING_LENGTH:
        raise ValidationError("Input too long")
    
    # Remove dangerous characters
    sanitized = data.strip()
    
    # Check for injection attempts
    if contains_sql_injection(sanitized):
        raise ValidationError("Invalid characters detected")
    
    return True
```

---

## 🧪 Testing Requirements

### 1. Unit Tests (MANDATORY)

**File:** `tests/test_your_server.py`

```python
import pytest
from mica.mcp_servers.your_server_mcp import your_tool

def test_your_tool_success():
    """Test successful execution."""
    result = your_tool(data="valid input")
    assert result["status"] == "success"
    assert "_metadata" in result
    assert result["_metadata"]["server_version"] == "1.0.0"

def test_your_tool_validation_error():
    """Test validation error handling."""
    with pytest.raises(ValidationError):
        your_tool(data="")

def test_your_tool_rate_limit():
    """Test rate limiting."""
    for i in range(100):
        result = your_tool(data=f"test {i}")
    
    # 101st call should fail
    result = your_tool(data="test 101")
    assert "error_type" in result
    assert result["error_type"] == "rate_limit_exceeded"

def test_your_tool_metadata():
    """Test metadata inclusion."""
    result = your_tool(data="test")
    assert "_metadata" in result
    assert "server_version" in result["_metadata"]
    assert "timestamp" in result["_metadata"]
```

---

### 2. Integration Tests (MANDATORY)

```python
async def test_server_integration():
    """Test full server integration."""
    from mica.mcp_servers.your_server_mcp import your_server
    
    # Get tools
    tools = await your_server.get_tools()
    assert len(tools) > 0
    
    # Call tool
    tool = tools["your_tool"]
    result = tool.fn(data="test")
    
    assert result["status"] == "success"
```

---

### 3. Load Tests (RECOMMENDED)

```python
def test_load():
    """Test server under load."""
    import concurrent.futures
    
    def call_tool(i):
        return your_tool(data=f"test {i}")
    
    # 100 concurrent calls
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(call_tool, i) for i in range(100)]
        results = [f.result() for f in futures]
    
    success_count = sum(1 for r in results if r.get("status") == "success")
    assert success_count >= 80  # At least 80% success rate
```

---

## 📚 Documentation Requirements

### 1. Server Documentation (MANDATORY)

**File:** `docs/YOUR_SERVER_MCP.md`

**Required Sections:**
```markdown
# Server Name MCP

## Overview
Brief description (2-3 sentences)

## Architecture
- Server type (IN_PROCESS or subprocess)
- Communication method
- Dependencies

## Tools
### tool_name_1
- **Description:** What it does
- **Parameters:** List with types and constraints
- **Returns:** Output format
- **Example:** Code example

## Installation
Steps to install dependencies

## Configuration
How to configure in mcp_servers.json

## Rate Limits
- Tool 1: 100 calls/min
- Tool 2: 20 calls/min (heavy)

## Error Handling
List of error types and meanings

## Examples
3-5 practical examples

## Performance
- Latency benchmarks
- Throughput estimates

## Changelog
Version history
```

---

### 2. Usage Guide (RECOMMENDED)

**File:** `docs/YOUR_SERVER_GUIDE.md`

**Required Sections:**
- Quick Start
- Common Use Cases
- Best Practices
- Troubleshooting
- FAQ

---

## 📖 Examples

### Example 1: Simple Calculator (IN_PROCESS)

```python
@calculator_server.tool()
@with_rate_limit()
@with_logging
@with_metadata
def add_numbers(a: float, b: float) -> Dict[str, Any]:
    """
    Add two numbers.
    
    Args:
        a: First number
        b: Second number
        
    Returns:
        Dictionary with sum and metadata
    """
    return {
        "a": a,
        "b": b,
        "sum": a + b,
        "operation": "addition"
    }
```

---

### Example 2: API Wrapper (Subprocess)

```python
def search_database(query: str, limit: int = 10) -> Dict[str, Any]:
    """
    Search external database via API.
    
    Args:
        query: Search query
        limit: Max results (1-100)
        
    Returns:
        Dictionary with search results
    """
    try:
        # Call external API
        response = requests.get(
            "https://api.example.com/search",
            params={"q": query, "limit": limit},
            timeout=30
        )
        response.raise_for_status()
        
        return {
            "query": query,
            "results": response.json(),
            "count": len(response.json()),
            "timestamp": datetime.utcnow().isoformat() + "Z"
        }
    except requests.Timeout:
        return {
            "error": "API request timeout",
            "error_type": "timeout_error",
            "recoverable": True,
            "suggestion": "Retry with smaller limit"
        }
    except Exception as e:
        return {
            "error": str(e),
            "error_type": "api_error",
            "recoverable": False
        }
```

---

## ✅ Checklist

Before submitting a new MCP server, ensure:

### Code Quality
- [ ] Follows naming conventions
- [ ] Custom exception hierarchy implemented
- [ ] Input validation with Pydantic
- [ ] Rate limiting configured
- [ ] Structured logging added
- [ ] Version metadata included
- [ ] All tools documented with docstrings
- [ ] Type hints on all functions
- [ ] No hardcoded credentials/secrets

### Testing
- [ ] Unit tests written (>80% coverage)
- [ ] Integration tests pass
- [ ] Load tests pass (if applicable)
- [ ] Error handling tested
- [ ] Rate limiting tested

### Documentation
- [ ] Server documentation created
- [ ] Usage guide written (if complex)
- [ ] README updated
- [ ] Configuration example provided
- [ ] API reference complete

### Configuration
- [ ] Entry in `mcp_servers.json`
- [ ] Correct mode (in_process/subprocess)
- [ ] Priority set (GOLD/SILVER/BRONZE)
- [ ] Tools count accurate

### Security
- [ ] Input sanitization implemented
- [ ] Rate limits appropriate
- [ ] No SQL/command injection vulnerabilities
- [ ] Sensitive data handling reviewed

---

## 🚀 Deployment

### 1. Add to Configuration

```json
{
  "your_server": {
    "command": "IN_PROCESS",
    "args": ["mica.mcp_servers.your_server_mcp:your_server"],
    "mode": "in_process",
    "tools_count": 15,
    "priority": "GOLD-2",
    "description": "Your server description",
    "category": "your_category"
  }
}
```

### 2. Test Integration

```bash
# Run test suite
python -m pytest tests/test_your_server.py -v

# Run integration test
python test_your_server_integration.py
```

### 3. Update Documentation

- Add entry to main README
- Update tool count in config
- Add examples to user guide

---

## 📞 Support

For questions or issues:
- Check existing servers for examples
- Review this guide
- Contact MICA development team

---

**Last Updated:** December 15, 2025  
**Maintained by:** MICA Platform Team  
**Version:** 1.0.0
