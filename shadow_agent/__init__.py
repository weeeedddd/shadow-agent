"""Shadow Agent -- a local, terminal-based LLM harness.

Three modules, run in strict sequence:

    Monarch    analysis      scans the ground, rewrites the request
    Eminence   execution     runs the commands, writes the files
    Architect  versioning    snapshots, journals, restores

Nothing in this package simulates work. Every value the interface prints is
read from the machine it is running on.
"""

__version__ = "0.3.0"
__all__ = ["__version__"]
