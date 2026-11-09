#!/usr/bin/env python3
"""Checkout wrapper for the acceptance demo also shipped in the installed wheel.

Use --output-root /tmp/new-proof for retained evidence; --provider codex for real models.
Fixture/offline modes explicitly cannot prove live model behavior.
"""
from hyperwilfred_core.foundry_acceptance import main

if __name__ == "__main__":
    raise SystemExit(main())
