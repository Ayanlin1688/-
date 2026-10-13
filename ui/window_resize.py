"""Logical resize hot zones shared by native hit testing and regressions."""

from PyQt5.QtCore import Qt


RESIZE_BORDER = 8
RESIZE_CORNER = 16

_HIT_TESTS = {
    Qt.LeftEdge: 10,
    Qt.RightEdge: 11,
    Qt.TopEdge: 12,
    Qt.TopEdge | Qt.LeftEdge: 13,
    Qt.TopEdge | Qt.RightEdge: 14,
    Qt.BottomEdge: 15,
    Qt.BottomEdge | Qt.LeftEdge: 16,
    Qt.BottomEdge | Qt.RightEdge: 17,
}


def resize_edges(point, size):
    """Return eight-direction Qt edges, excluding the interior and outside points."""
    horizontal, vertical = point.x(), point.y()
    width, height = size.width(), size.height()
    if not (0 <= horizontal < width and 0 <= vertical < height):
        return Qt.Edges()
    left_corner = horizontal < RESIZE_CORNER
    right_corner = horizontal >= width - RESIZE_CORNER
    top_corner = vertical < RESIZE_CORNER
    bottom_corner = vertical >= height - RESIZE_CORNER
    if left_corner and top_corner:
        return Qt.TopEdge | Qt.LeftEdge
    if right_corner and top_corner:
        return Qt.TopEdge | Qt.RightEdge
    if left_corner and bottom_corner:
        return Qt.BottomEdge | Qt.LeftEdge
    if right_corner and bottom_corner:
        return Qt.BottomEdge | Qt.RightEdge
    if horizontal < RESIZE_BORDER:
        return Qt.LeftEdge
    if horizontal >= width - RESIZE_BORDER:
        return Qt.RightEdge
    if vertical < RESIZE_BORDER:
        return Qt.TopEdge
    if vertical >= height - RESIZE_BORDER:
        return Qt.BottomEdge
    return Qt.Edges()


def resize_hit_test(point, size):
    """Return a Win32 HT resize code, or zero when this is not a resize hot zone."""
    return _HIT_TESTS.get(resize_edges(point, size), 0)
