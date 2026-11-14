"""Structural capture of ripple-control *command* telegrams (receive-only, research).

Goal: preserve enough structure in the dataset that a later analysis — once a
large corpus is collected — can learn **which telegrams switch RRC devices on and
off**, by correlating telegram content with observed device behaviour.

This module does NOT claim to fully decode the proprietary Versacom
(DIN 43861-401) / Semagyr-TOP (DIN 43861-402) programs — the BlinkenCity team
needed vendor software and fuzzing for that. It extracts only what is reliably
structural (control byte, EVU address, command byte, argument bytes) and derives
**switch-command hypotheses** from the public fact that a Semagyr Direktbefehl
encodes a DoppelKommando (DK) and its state by bit position: the 1-indexed bit
position ``p`` maps to ``DK = (p+1)//2`` with state ON for odd ``p`` and OFF for
even ``p`` (so bit 16 set ⇒ DK 8, OFF). These are labelled hypotheses, to be
validated against observed switching — never acted upon, and nothing is transmitted.
"""
from __future__ import annotations

from .framing import Frame

# Bit ordering for switch-command hypotheses: bytes in order, MSB-first within a
# byte, positions 1-indexed. Documented and fixed so the dataset is reproducible.


def set_bit_positions(data: bytes) -> list[int]:
    positions: list[int] = []
    for byte_index, byte in enumerate(data):
        for bit in range(8):                       # MSB first
            if byte & (0x80 >> bit):
                positions.append(byte_index * 8 + bit + 1)
    return positions


def switch_hypotheses(args: bytes) -> list[dict]:
    """Candidate (double-command, state) switch actions implied by set bit positions.

    HYPOTHESIS ONLY: based on the Semagyr Direktbefehl bit-position encoding
    described publicly; validate against observed device behaviour before trusting.
    """
    out = []
    for p in set_bit_positions(args):
        out.append({"bit": p, "double_command": (p + 1) // 2,
                    "state": "ON" if p % 2 == 1 else "OFF"})
    return out


def decode_command(frame: Frame) -> dict:
    """Structural fields of a command telegram (header-level; payload not interpreted)."""
    payload = frame.payload
    command_byte = payload[0] if payload else None
    args = payload[1:] if len(payload) > 1 else b""
    return {
        "control": f"{frame.control:02x}",
        "evu_address": f"{frame.address:04x}" if frame.address >= 0 else None,
        "command_byte": f"{command_byte:02x}" if command_byte is not None else None,
        "args_hex": args.hex(),
        "n_args": len(args),
        # derived, for the on/off study (clearly a hypothesis):
        "switch_hypotheses": switch_hypotheses(args),
        "note": "structural capture only; switch_hypotheses are unverified",
    }
