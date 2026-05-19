"""
skill_loader.py — EduAnimator SKILL.md Integration
═══════════════════════════════════════════════════
HOW IT WORKS:
  1. Reads SKILL.md from disk (once at startup)
  2. Prepends it to EVERY system prompt as a cached block
  3. Anthropic caches the SKILL.md content — you only pay
     for it on first call. Subsequent calls = 90% cheaper.

HOW TO USE IN claude_client.py:
  from skill_loader import build_system_with_skill

  # Replace any:
  system="Your system prompt here"
  # With:
  system=build_system_with_skill("Your system prompt here")

SKILL.md LOCATION PRIORITY:
  1. Same folder as claude_client.py  → ./SKILL.md
  2. Parent folder                    → ../SKILL.md
  3. skills/ subfolder               → ./skills/SKILL.md
  4. Fallback: empty string (no crash)
"""

import os
from pathlib import Path

# ── Find SKILL.md ─────────────────────────────────────────────────────

def _find_skill_md() -> str:
    """Search common locations for SKILL.md. Returns content or ''."""
    search_paths = [
        Path(__file__).parent / "SKILL.md",           # same folder as this file
        Path(__file__).parent.parent / "SKILL.md",    # parent folder
        Path(__file__).parent / "skills" / "SKILL.md",# skills/ subfolder
        Path.cwd() / "SKILL.md",                       # current working directory
    ]
    for p in search_paths:
        if p.exists():
            content = p.read_text(encoding="utf-8").strip()
            print(f"[SKILL]   Loaded from: {p}  ({len(content):,} chars)")
            return content
    print("[SKILL]   ⚠️  SKILL.md not found — generating without curriculum guide")
    return ""

# Load once at import time
_SKILL_CONTENT: str = _find_skill_md()


# ── Build cached system prompt ─────────────────────────────────────────

def build_system_with_skill(base_system: str) -> list:
    """
    Returns a system prompt list with SKILL.md as a CACHED prefix block.

    Usage:
        system=build_system_with_skill("Your instructions here")

    Anthropic prompt caching:
        - First call:  SKILL.md tokens are written to cache (~$0.00375/MTok)
        - All calls:   Cached tokens cost 90% less (~$0.0003/MTok)
        - Cache TTL:   5 minutes (refreshed on each use)

    Returns:
        List of content blocks for the `system` parameter.
        If SKILL.md is empty, returns a plain string (backward-compatible).
    """
    if not _SKILL_CONTENT:
        # No SKILL.md found — return plain string, API still works
        return base_system

    return [
        {
            "type": "text",
            "text": f"# CURRICULUM BIBLE — READ THIS FIRST\n\n{_SKILL_CONTENT}\n\n---\n",
            "cache_control": {"type": "ephemeral"}   # ← tells Anthropic to cache this block
        },
        {
            "type": "text",
            "text": base_system
        }
    ]


def get_skill_content() -> str:
    """Returns raw SKILL.md content (for debugging or display)."""
    return _SKILL_CONTENT


def skill_loaded() -> bool:
    """True if SKILL.md was found and loaded."""
    return bool(_SKILL_CONTENT)