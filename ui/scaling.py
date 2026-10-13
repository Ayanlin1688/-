"""Read screen metrics without applying any interface scaling."""


def screen_factors(screen) -> dict[str, float | int]:
    """Return DPI / 96 and Qt logical screen/work-area dimensions, without re-scaling."""
    size = screen.size()
    available = screen.availableGeometry()
    return {
        'dpi': float(screen.logicalDotsPerInch()) / 96.0,
        'logical_width': size.width(),
        'logical_height': size.height(),
        'available_width': available.width(),
        'available_height': available.height(),
    }
