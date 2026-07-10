"""S2.8 — Agent Skills Filesystem: progressive disclosure via SKILL.md files.

SKILL.md files live under ``drivers/skills/`` and contain a YAML
front-matter header followed by Markdown content.  The ``SkillLoader``
reads, parses, and serves them by tier level:

- **Tier 0 (core)**: Always injected — safety, identity, formatting.
- **Tier 1 (domain)**: Loaded when query keywords match the skill's
  keyword list.
- **Tier 2 (advanced)**: Loaded only on explicit request.

Usage::

    loader = SkillLoader()                    # auto-discovers from skills/
    loader = SkillLoader.from_directory(path)  # custom path

    # Get all T0 skills (always-on)
    core = loader.get_tier(0)

    # Get T0 + matching T1 skills for a query
    relevant = loader.resolve("binding site docking study")

    # Get everything (T0 + T1 + T2) for a given domain
    all_md = loader.get_domain("structural_biology")
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


# ── Skill metadata ─────────────────────────────────────────────────────
@dataclass(frozen=True)
class SkillMeta:
    """Parsed front-matter of a SKILL.md file."""

    name: str
    tier: int  # 0, 1, or 2
    domain: str
    keywords: tuple  # immutable keyword list
    description: str
    content: str  # full Markdown body (after front-matter)
    tool_names: tuple = ()
    source_path: str = ""  # filesystem path for debugging

    def matches_query(self, query: str) -> bool:
        """Return True if any keyword appears in the query (case-insensitive)."""
        q_lower = query.lower()
        return any(kw.lower() in q_lower for kw in self.keywords)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "tier": self.tier,
            "domain": self.domain,
            "keywords": list(self.keywords),
            "tool_names": list(self.tool_names),
            "description": self.description,
            "content_length": len(self.content),
            "source_path": self.source_path,
        }


# ── YAML front-matter parser (minimal, no PyYAML dependency) ──────────
_FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_LIST_RE = re.compile(r"\[(.*?)\]")


def _parse_front_matter(raw: str) -> tuple[Dict[str, Any], str]:
    """Parse a minimal YAML front-matter block from a Markdown file.

    Returns (metadata_dict, body_text).  Supports simple ``key: value``
    and ``key: [a, b, c]`` list syntax.  Does NOT require PyYAML.
    """
    m = _FRONT_MATTER_RE.match(raw)
    if not m:
        return {}, raw

    header = m.group(1)
    body = raw[m.end():]
    meta: Dict[str, Any] = {}

    for line in header.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()

        # Handle list values  [a, b, c]
        list_match = _LIST_RE.search(val)
        if list_match:
            items = [x.strip().strip("'\"") for x in list_match.group(1).split(",")]
            meta[key] = [x for x in items if x]
        else:
            # Scalar — strip quotes
            meta[key] = val.strip("'\"")

    return meta, body


# ── SkillLoader ────────────────────────────────────────────────────────
class SkillLoader:
    """Loads and serves SKILL.md files for progressive disclosure."""

    def __init__(self, skills: Optional[Sequence[SkillMeta]] = None) -> None:
        self._skills: List[SkillMeta] = list(skills) if skills else []

    # ── factory ────────────────────────────────────────────────────
    @classmethod
    def from_directory(cls, directory: str | Path) -> "SkillLoader":
        """Discover and parse all ``*.md`` files in *directory*."""
        dirpath = Path(directory)
        skills: List[SkillMeta] = []
        if not dirpath.is_dir():
            return cls(skills)

        for fp in sorted(dirpath.glob("*.md")):
            if fp.name.upper() == "README.MD":
                continue
            try:
                raw = fp.read_text(encoding="utf-8")
                meta_dict, body = _parse_front_matter(raw)
                if not meta_dict.get("name"):
                    continue  # skip files without valid front-matter
                skill = SkillMeta(
                    name=meta_dict.get("name", fp.stem),
                    tier=int(meta_dict.get("tier", 1)),
                    domain=meta_dict.get("domain", "general"),
                    keywords=tuple(meta_dict.get("keywords", [])),
                    tool_names=tuple(meta_dict.get("tool_names", [])),
                    description=meta_dict.get("description", ""),
                    content=body.strip(),
                    source_path=str(fp),
                )
                skills.append(skill)
            except Exception:
                continue  # skip unparseable files

        return cls(skills)

    @classmethod
    def default(cls) -> "SkillLoader":
        """Load from the built-in ``skills/`` directory next to this file."""
        skills_dir = Path(__file__).parent / "skills"
        return cls.from_directory(skills_dir)

    # ── queries ────────────────────────────────────────────────────
    def get_tier(self, tier: int) -> List[SkillMeta]:
        """Return all skills at the given tier level."""
        return [s for s in self._skills if s.tier == tier]

    def get_domain(self, domain: str) -> List[SkillMeta]:
        """Return all skills matching the given domain."""
        return [s for s in self._skills if s.domain == domain]

    def get_by_name(self, name: str) -> Optional[SkillMeta]:
        """Return the skill with the given name, or None."""
        for s in self._skills:
            if s.name == name:
                return s
        return None

    def resolve(self, query: str, *, include_tier_2: bool = False) -> List[SkillMeta]:
        """Return T0 skills + T1 skills whose keywords match the query.

        If *include_tier_2* is True, also include matching T2 skills.
        """
        result: List[SkillMeta] = []
        for s in self._skills:
            if s.tier == 0:
                result.append(s)
            elif s.tier == 1 and s.matches_query(query):
                result.append(s)
            elif s.tier == 2 and include_tier_2 and s.matches_query(query):
                result.append(s)
        return result

    def render(self, skills: Sequence[SkillMeta]) -> str:
        """Concatenate skill contents into a single prompt-injectable string."""
        parts: List[str] = []
        for s in skills:
            parts.append(f"## SKILL: {s.name} (T{s.tier})\n\n{s.content}")
        return "\n\n---\n\n".join(parts)

    # ── introspection ──────────────────────────────────────────────
    @property
    def count(self) -> int:
        return len(self._skills)

    def list_names(self) -> List[str]:
        return [s.name for s in self._skills]

    def summary(self) -> str:
        by_tier = {0: 0, 1: 0, 2: 0}
        for s in self._skills:
            by_tier[s.tier] = by_tier.get(s.tier, 0) + 1
        return (
            f"SkillLoader: {self.count} skills "
            f"(T0={by_tier[0]}, T1={by_tier[1]}, T2={by_tier[2]})"
        )
