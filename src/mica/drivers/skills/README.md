# Agent Skills — Progressive Disclosure

This directory contains **SKILL.md** files that describe agent capabilities
for progressive disclosure.  The `SkillLoader` reads these files and provides
them to agents based on tier level.

## 3-Tier Model

| Tier | When loaded | Content |
|------|-------------|---------|
| **T0: Core** | Always injected into system prompt | Essential capabilities, safety boundaries |
| **T1: Domain** | Loaded when domain keyword matches | Domain-specific knowledge (e.g., docking, MD) |
| **T2: Advanced** | Loaded on explicit request | Rare / expert procedures |

## File Format

Each `SKILL.md` file has a YAML front-matter header:

```yaml
---
name: molecular_docking
tier: 1
domain: structural_biology
keywords: [docking, vina, autodock, glide]
description: How to set up and run molecular docking calculations
---
```

Followed by Markdown content describing the skill.
