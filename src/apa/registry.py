"""Registry bridge: wraps singapore's jd_registry_ops.py via MCP (FastMCP).

Iron rules: all JD create/read/update/delete go through the single channel;
no shell pass-through for Chinese text - use the runpy/argv structure.
Planned to land in D6; acceptance = one ledger update completed via MCP.
"""
