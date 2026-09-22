from __future__ import annotations

from voidcode.hook.surfaces import HOOK_SURFACE_DESCRIPTORS


def test_hook_catalog_has_no_prespawn_surface() -> None:
    surfaces = [descriptor.surface for descriptor in HOOK_SURFACE_DESCRIPTORS]
    assert not [s for s in surfaces if "spawn" in s or "pre_spawn" in s]
