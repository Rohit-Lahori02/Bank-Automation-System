"""In-memory member and account store seeded from data/members.json.

All records are synthetic. There is no real PII anywhere in this file or the
seed data; the "tax_id_last4" field exists only so the redaction layer has
something realistic to mask.
"""

from __future__ import annotations

import copy
import json
import secrets
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from threading import Lock

SEED_PATH = Path(__file__).parent / "data" / "members.json"

PRODUCTS = {
    "S-VAC": "Savings - Vacation Club",
    "S-HOL": "Savings - Holiday Club",
    "CD-12": "Certificate - 12 Month",
}

MIN_INITIAL_DEPOSIT = Decimal("25.00")


class MemberStore:
    def __init__(self, seed_path: Path = SEED_PATH) -> None:
        with open(seed_path, encoding="utf-8") as fh:
            raw = json.load(fh)
        self._members: dict[str, dict] = {m["member_id"]: m for m in raw["members"]}
        self._lock = Lock()

    # ---- reads -----------------------------------------------------------
    def get(self, member_id: str) -> dict | None:
        with self._lock:
            member = self._members.get(member_id)
            return copy.deepcopy(member) if member else None

    def exists(self, member_id: str) -> bool:
        with self._lock:
            return member_id in self._members

    def accounts(self, member_id: str) -> list[dict]:
        member = self.get(member_id)
        return member["accounts"] if member else []

    # ---- writes (the "irreversible" business actions) --------------------
    def open_subaccount(self, member_id: str, product: str, nickname: str, deposit: Decimal) -> dict:
        with self._lock:
            member = self._members[member_id]
            prefix = "C" if product.startswith("CD") else "S"
            existing = [a for a in member["accounts"] if a["account_no"].split("-")[1].startswith(prefix)]
            seq = len(existing) + 1
            account = {
                "account_no": f"{member_id}-{prefix}{seq:02d}",
                "type": "Certificate" if prefix == "C" else "Savings",
                "nickname": nickname,
                "balance": float(deposit),
                "status": "Open",
            }
            member["accounts"].append(account)
            confirmation = f"CNF-{secrets.token_hex(3).upper()}"
            return {"account": copy.deepcopy(account), "confirmation_no": confirmation}

    def close_account(self, member_id: str, account_no: str) -> bool:
        with self._lock:
            member = self._members.get(member_id)
            if not member:
                return False
            for account in member["accounts"]:
                if account["account_no"] == account_no and account["status"] == "Open":
                    account["status"] = "Closed"
                    account["balance"] = 0.0
                    return True
            return False


def parse_money(text: str) -> Decimal | None:
    cleaned = (text or "").replace("$", "").replace(",", "").strip()
    if not cleaned:
        return None
    try:
        return Decimal(cleaned).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:
        return None


def money(value) -> str:
    return f"${Decimal(str(value)):,.2f}"
