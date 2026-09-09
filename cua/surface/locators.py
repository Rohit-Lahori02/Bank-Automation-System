"""Locator strategies: how a recorded step identifies its target control.

A Target carries an ordered chain of strategies. Replay tries them in order and
records which one resolved. The order encodes a robustness judgement:

  1. role_name        Accessible role + name as a screen reader would compute it.
                      Survives restyling and DOM restructuring. Only recorded when
                      the name came from real accessibility semantics.
  2. text             Visible text (for static cells/headings/labels).
  3. label_text       Our own name inference including LEGACY fallbacks (adjacent
                      table cell, nearest text to the left/above). This is what
                      makes unlabeled inputs in table-based forms addressable.
  4. anchor_relative  Spatial: the nearest control of a role in a direction from an
                      anchor text. Works even when markup carries no association.
  5. table_cell       A grid cell addressed by its row anchor text and column
                      header, resolved geometrically. This is how a VALUE cell
                      (a balance, a status) is found on replay when the value
                      itself differs per invocation.
  6. css              Structural path / form field name attribute. Cheap and
                      precise on server-rendered apps, but brittle under drift.
  7. bbox             Coordinates relative to the recorded viewport. Works on any
                      surface, including screenshot-driven or desktop automation.

Each strategy carries a human-readable rationale so an artifact reviewer can see
why the recorder believed it would hold.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

from .snapshot import Element, Snapshot, TEXTUAL_ROLES

# Roles Playwright's get_by_role understands with the same semantics we infer.
PLAYWRIGHT_ROLES = {"link", "button", "textbox", "combobox", "checkbox", "radio", "heading", "cell", "img", "dialog"}
# Name sources that Playwright's accessible-name computation would also produce.
ACCESSIBLE_NAME_SOURCES = {"aria", "label", "content", "placeholder", "title"}


class _StrategyBase(BaseModel):
    frame: str = "main"
    nth: int = 0
    rationale: str = ""


class RoleNameStrategy(_StrategyBase):
    kind: Literal["role_name"] = "role_name"
    role: str
    name: str
    exact: bool = True


class TextStrategy(_StrategyBase):
    kind: Literal["text"] = "text"
    text: str
    exact: bool = True


class LabelTextStrategy(_StrategyBase):
    kind: Literal["label_text"] = "label_text"
    role: str
    text: str


class AnchorRelativeStrategy(_StrategyBase):
    kind: Literal["anchor_relative"] = "anchor_relative"
    role: str
    anchor_text: str
    direction: Literal["right", "below"] = "right"


class TableCellStrategy(_StrategyBase):
    kind: Literal["table_cell"] = "table_cell"
    row_text: str          # text of a cell that identifies the row (e.g. "Primary Share")
    column_header: str     # text of the header cell above the wanted column (e.g. "Balance")


class CssStrategy(_StrategyBase):
    kind: Literal["css"] = "css"
    selector: str


class BBoxStrategy(_StrategyBase):
    kind: Literal["bbox"] = "bbox"
    role: str
    x: float
    y: float
    w: float
    h: float
    viewport_w: int
    viewport_h: int


Strategy = Annotated[
    Union[RoleNameStrategy, TextStrategy, LabelTextStrategy, AnchorRelativeStrategy, TableCellStrategy,
          CssStrategy, BBoxStrategy],
    Field(discriminator="kind"),
]


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return min(a1, b1) - max(a0, b0)


def grid_context(el: Element, elements: list[Element]) -> tuple[Element | None, Element | None]:
    """For a cell, find (row anchor, column header) within the same grid, geometrically.

    Row anchor: the leftmost textual cell in the same row that is not the element.
    Column header: the topmost cell in the same column of the grid.
    """
    if el.role != "cell" or el.group is None:
        return None, None
    peers = [e for e in elements if e.role == "cell" and e.group == el.group and e.frame == el.frame and e is not el]
    row = [e for e in peers if _overlap(e.bbox.y, e.bbox.bottom, el.bbox.y, el.bbox.bottom) > min(e.bbox.h, el.bbox.h) * 0.5
           and e.bbox.x < el.bbox.x and e.name]
    col = [e for e in peers if _overlap(e.bbox.x, e.bbox.right, el.bbox.x, el.bbox.right) > min(e.bbox.w, el.bbox.w) * 0.5
           and e.bbox.y < el.bbox.y and e.name]
    row_anchor = min(row, key=lambda e: e.bbox.x) if row else None
    header = min(col, key=lambda e: e.bbox.y) if col else None
    # A two-column grid is a key/value layout: its first row is data, not headers. Likewise a
    # "header" without a single letter (e.g. "12345") is a value.
    columns = {round((e.bbox.x + e.bbox.w / 2) / 20) for e in [*peers, el]}
    if header is not None and (len(columns) <= 2 or not any(ch.isalpha() for ch in header.name)):
        header = None
    return row_anchor, header


def left_label(el: Element, elements: list[Element], max_gap: float = 320.0) -> Element | None:
    """The nearest textual element to the left of `el` on the same line: its label in a key/value layout."""
    best, best_gap = None, None
    for e in elements:
        if e is el or e.frame != el.frame or e.role not in TEXTUAL_ROLES or not e.name:
            continue
        if e.bbox.right > el.bbox.x + 6:
            continue
        if _overlap(e.bbox.y, e.bbox.bottom, el.bbox.y, el.bbox.bottom) < min(e.bbox.h, el.bbox.h) * 0.5:
            continue
        gap = el.bbox.x - e.bbox.right
        if gap <= max_gap and (best_gap is None or gap < best_gap):
            best, best_gap = e, gap
    return best


class Target(BaseModel):
    """What a step acts on, plus the evidence for how to find it again."""

    description: str
    role: str
    strategies: list[Strategy]


def build_target(el: Element, snapshot: Snapshot) -> Target:
    """Derive a strategy chain for an element observed in a snapshot (record time)."""
    strategies: list[Strategy] = []
    frame = el.frame
    same_frame = [e for e in snapshot.elements if e.frame == frame]

    def nth_among(pred) -> int:
        matches = [e for e in same_frame if pred(e)]
        return matches.index(el) if el in matches else 0

    row_anchor, header = grid_context(el, snapshot.elements)
    if row_anchor is not None and header is not None:
        strategies.append(TableCellStrategy(
            frame=frame, row_text=row_anchor.name, column_header=header.name,
            rationale=f"grid cell in the row labelled '{row_anchor.name}' under the column '{header.name}'; "
                      "independent of the cell's current value, so it survives different inputs on replay",
        ))

    if el.name and el.role in PLAYWRIGHT_ROLES and el.name_source in ACCESSIBLE_NAME_SOURCES:
        strategies.append(RoleNameStrategy(
            frame=frame, role=el.role, name=el.name, exact=True,
            nth=nth_among(lambda e: e.role == el.role and e.name == el.name),
            rationale=f"accessible {el.role} named '{el.name}' (source: {el.name_source}); "
                      "independent of styling and DOM structure",
        ))

    if el.role in TEXTUAL_ROLES:
        label = left_label(el, snapshot.elements)
        covered_by_table_cell = label is row_anchor and header is not None
        if label is not None and not covered_by_table_cell:
            strategies.append(AnchorRelativeStrategy(
                frame=frame, role=el.role, anchor_text=label.name, direction="right",
                rationale=f"the {el.role} to the right of the label '{label.name}'; independent of this "
                          "value's content, so an extracted value can be re-read for other inputs",
            ))

    if el.name and el.role in TEXTUAL_ROLES:
        strategies.append(TextStrategy(
            frame=frame, text=el.name, exact=True,
            nth=nth_among(lambda e: e.role in TEXTUAL_ROLES and e.name == el.name),
            rationale="visible text content; what the operator reads on screen",
        ))

    if el.name and el.role not in TEXTUAL_ROLES:
        strategies.append(LabelTextStrategy(
            frame=frame, role=el.role, text=el.name,
            nth=nth_among(lambda e: e.role == el.role and e.name == el.name),
            rationale=f"{el.role} whose inferred label is '{el.name}' (source: {el.name_source}); "
                      "uses adjacent-cell/nearest-text inference for legacy forms without <label for>",
        ))

    if el.name and el.name_source.startswith("adjacent"):
        direction = "below" if el.name_source == "adjacent_above" else "right"
        strategies.append(AnchorRelativeStrategy(
            frame=frame, role=el.role, anchor_text=el.name, direction=direction,
            rationale=f"spatial: nearest {el.role} to the {direction} of the text '{el.name}'; "
                      "survives table restructuring as long as the visual layout holds",
        ))

    if el.name_attr and el.tag in {"input", "select", "textarea"}:
        strategies.append(CssStrategy(
            frame=frame, selector=f'{el.tag}[name="{el.name_attr}"]',
            rationale="form field name attribute; stable in server-rendered apps, invisible to the operator",
        ))
    elif el.href and el.role == "link":
        strategies.append(CssStrategy(
            frame=frame, selector=f'a[href="{el.href}"]',
            rationale="link target; stable while routes are stable (parameterized on replay)",
        ))

    if el.css:
        strategies.append(CssStrategy(
            frame=frame, selector=el.css,
            rationale="structural DOM path; brittle under layout drift, kept as a late fallback",
        ))

    strategies.append(BBoxStrategy(
        frame=frame, role=el.role, x=el.bbox.x, y=el.bbox.y, w=el.bbox.w, h=el.bbox.h,
        viewport_w=snapshot.viewport[0], viewport_h=snapshot.viewport[1],
        rationale="coordinates relative to the recorded viewport; surface-agnostic last resort "
                  "(screenshot/desktop automation)",
    ))

    description = f'{el.role} "{el.name}"' if el.name else f"{el.role} <{el.tag}>"
    if frame != "main":
        description += f" in frame {frame}"
    return Target(description=description, role=el.role, strategies=strategies)
