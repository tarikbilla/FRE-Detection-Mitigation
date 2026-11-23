"""Optical (IR) port of the FRE: read-only IEC 62056-21 probe."""
from .optical import IrProbe, parse_data_block, parse_ident, strip_echo

__all__ = ["IrProbe", "parse_data_block", "parse_ident", "strip_echo"]
