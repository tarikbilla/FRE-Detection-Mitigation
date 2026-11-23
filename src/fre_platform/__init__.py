"""FRE (radio ripple control) receive-only detection & mitigation research platform.

This package only *receives* radio signals. It contains no code path that
transmits, and the supported receiver (Airspy HF+) has no transmitter.
"""

__version__ = "2.2.0"

#: Version of the MongoDB document layout. Bump when fields change incompatibly.
SCHEMA_VERSION = 1
