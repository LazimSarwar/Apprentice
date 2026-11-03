"""Object semantics shared by extraction, checking and simulation."""
CONTAINER_WORDS = ("tray", "bin", "bowl", "plate", "container", "box")


def is_container(obj) -> bool:
    if getattr(obj, "kind", None) is not None:
        return obj.kind == "container"
    text = f"{obj.id} {obj.shape or ''}".lower()
    return any(word in text for word in CONTAINER_WORDS)
