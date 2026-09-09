"""Surface layer: perceive and act on a live UI through one seam."""

from .base import Resolved, Surface, TargetNotFound
from .driver import BrowserSurface
from .locators import (
    AnchorRelativeStrategy, BBoxStrategy, CssStrategy, LabelTextStrategy, RoleNameStrategy, TableCellStrategy,
    Target, TextStrategy, build_target, grid_context,
)
from .session import ControlError, Controller, SessionControl
from .snapshot import BBox, Element, Snapshot

__all__ = [
    "AnchorRelativeStrategy", "BBox", "BBoxStrategy", "BrowserSurface", "ControlError", "Controller",
    "CssStrategy", "Element", "LabelTextStrategy", "Resolved", "RoleNameStrategy", "SessionControl",
    "Snapshot", "Surface", "TableCellStrategy", "Target", "TargetNotFound", "TextStrategy", "build_target",
    "grid_context",
]
