"""Coll — reverse-engineer features from apps.

Apple-native first, stdlib-only orchestrator, evidence-backed findings.
Lanes: apple (Mach-O / .app), electron (Electron/JS bundles).
"""
from .shell import run, slug
from .evidence import EvidenceBank, Finding

__all__ = ["run", "slug", "EvidenceBank", "Finding"]
