from __future__ import annotations


def linked_key(program, version: str = "") -> str:
    """Per-machine Software Linker config key for a Program (+ version).
    Stays plain `program.id` for a single/no-version Program (preserves
    already-linked paths from before multi-version support existed);
    becomes "<id>:<version>" once a Program has multiple versions, since
    each version needs its own linked executable. Convention-only
    duplicate of the same logic in
    plugins/core/software_linker/plugin.py — keep both in sync if this
    shape ever changes, same discipline as MAYA_ENV_BRIDGE_PLUGIN_ID."""
    if len(program.versions) <= 1:
        return program.id
    return f"{program.id}:{version}"


def pinned_version(repo, program) -> str:
    """Which version of a multi-version Program this repo launches —
    repo.program_version_pins wins if it names a version the Program still
    has; otherwise the catalog's first version (or "" if it has none)."""
    pin = repo.program_version_pins.get(program.id)
    if pin and pin in program.versions:
        return pin
    return program.versions[0] if program.versions else ""
