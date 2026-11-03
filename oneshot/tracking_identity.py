"""Detect when separate text queries have latched onto the same physical item.

Near-identical, sustained trajectories are ambiguous evidence, even if the service
assigned different object IDs. Brief overlap during stacking is not sufficient.
"""
from itertools import combinations


def _iou(a, b):
    intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    union = (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - intersection
    return intersection / union if union else 0


def ambiguous_tracks(objects, start: float = 0, end: float = float('inf')) -> dict[str, set[str]]:
    """Return names that cannot be independently grounded in the saved tracks."""
    result = {}
    for a, b in combinations([o for o in objects if o.track and o.track.boxes], 2):
        left = {box.frame: box for box in a.track.boxes if start <= box.t_s <= end}
        right = {box.frame: box for box in b.track.boxes if start <= box.t_s <= end}
        if not left or not right:
            continue
        exact = a.track.boxes == b.track.boxes
        common = sorted(left.keys() & right.keys())
        # Require overlap across most of both tracks, not just a shared final pose.
        duplicate = (len(common) >= 3
                     and len(common) >= .8 * max(len(left), len(right))
                     and left[common[-1]].t_s - left[common[0]].t_s >= .5
                     and sum(_iou(left[f].box, right[f].box) >= .85 for f in common) >= .8 * len(common))
        if exact or duplicate:
            result.setdefault(a.id, set()).add(b.id)
            result.setdefault(b.id, set()).add(a.id)
    return result
