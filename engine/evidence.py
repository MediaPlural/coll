"""Evidence contract: every finding ships with provenance, not vibes.

Law (mirrors the Consumer + REA's own discipline, synthesized our way):
  - no conclusion without the evidence artifact + exact reproduction command
  - digest pins the artifact so findings can be re-verified byte-for-byte
  - confidence is explicit: observed | inferred | unknown
  - limitations are stated, never papered over
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, asdict, field

from .shell import slug


@dataclass
class Finding:
    lane: str
    target: str
    label: str
    tool: str
    command: str
    summary: object = None
    artifact: str = ""          # filename relative to bank root
    digest: str = ""            # blake2b-128 of the raw artifact
    confidence: str = "observed"
    limitation: str = ""


class EvidenceBank:
    """Collects findings, persists raw artifacts, emits a verifiable manifest."""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.findings: list[Finding] = []
        os.makedirs(self.root, exist_ok=True)

    def add(self, lane, target, label, tool, command, raw, summary=None,
            confidence="observed", limitation="", ext="txt") -> Finding:
        if isinstance(raw, str):
            raw = raw.encode()
        digest = hashlib.blake2b(raw, digest_size=16).hexdigest()
        fname = f"{len(self.findings):03d}-{slug(label)}.{ext}"
        with open(os.path.join(self.root, fname), "wb") as f:
            f.write(raw)
        fnd = Finding(
            lane=lane, target=target, label=label, tool=tool, command=command,
            summary=summary, artifact=fname, digest=digest,
            confidence=confidence, limitation=limitation,
        )
        self.findings.append(fnd)
        return fnd

    def save_manifest(self):
        with open(os.path.join(self.root, "manifest.json"), "w") as fh:
            json.dump([asdict(f) for f in self.findings], fh, indent=2, default=str)
        return os.path.join(self.root, "manifest.json")
