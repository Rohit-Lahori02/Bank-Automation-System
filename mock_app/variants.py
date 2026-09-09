"""Tenant variants of the same vendor product.

Hundreds of institutions run the same core system, each branded, configured and versioned a
little differently. The mock console models that with variants: the flows and routes are the
same product, but labels, menu names, theme, version and one piece of layout differ - the kind
of drift a capability recorded on one tenant meets on the next.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Variant:
    key: str
    institution: str
    version: str
    nav_inquiry: str        # menu / page title for the member lookup function
    search_label: str       # label next to the member number field
    search_button: str      # submit button text
    view_link: str          # link text in the results row
    balance_header: str     # accounts grid column header
    branch_select: bool     # an extra "Branch" row above the member number (shifts layout)
    theme: str              # css theme class


VARIANTS: dict[str, Variant] = {
    "harbor": Variant(
        key="harbor", institution="Harbor Federal Credit Union", version="4.2.1",
        nav_inquiry="Member Inquiry", search_label="Member Number", search_button="Search",
        view_link="View", balance_header="Balance", branch_select=False, theme="navy",
    ),
    "lakeshore": Variant(
        key="lakeshore", institution="Lakeshore Community Credit Union", version="4.3.0",
        nav_inquiry="Member Lookup", search_label="Member No.", search_button="Find",
        view_link="Open", balance_header="Current Balance", branch_select=True, theme="green",
    ),
}


def get_variant(key: str) -> Variant:
    try:
        return VARIANTS[key]
    except KeyError:
        raise ValueError(f"unknown mock app variant '{key}' (choose from {sorted(VARIANTS)})") from None
