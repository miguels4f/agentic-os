"""Read-only command center: one snapshot of a workspace's projects and runs.

The package only READS the workspace. It never imports the engine, never opens a network
connection, and reads git exclusively through the allowlist in ``gitread``. The only directory
it writes is the output directory, which must lie outside the workspace.
"""

from __future__ import annotations

__all__: list[str] = []
