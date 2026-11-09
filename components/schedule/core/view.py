"""Read-only configuration projection for the authenticated AstrBot plugin view."""

from __future__ import annotations


def build_wardrobe_payload(wardrobe, image_store, *, entries=None) -> dict:
    """Read the current panel order and attach only existing local thumbnails."""
    snapshot = wardrobe.display_entries() if entries is None else entries
    result = []
    for number, entry in enumerate(snapshot, 1):
        entry_id = str(entry.get("id", ""))
        try:
            image = image_store.data_uri(entry_id) if image_store else ""
        except (OSError, ValueError):
            image = ""
        result.append(
            {
                "id": entry_id,
                "number": number,
                "description": str(entry.get("description", "") or ""),
                "category": str(entry.get("category", "外出") or "外出"),
                "seasons": list(entry.get("seasons", [])),
                "image": image,
            }
        )
    return {"entries": result}
