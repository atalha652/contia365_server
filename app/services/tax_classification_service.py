"""
Tax Classification Layer
========================
Sits between the OCR/accounting pipeline and the tax engine.

Responsibility:
  Given a ledger entry, determine which Spanish tax modelos it belongs to
  by matching financial signals against the user's census obligations and
  the modelos collection — with zero hardcoded modelo numbers.

Pipeline:
  OCR → ledger insert → classify_ledger_entry() → ledger.tax_classification persisted
                                                          ↓
                                              Tax engine reads modelo_ids only

Signal extraction (from ledger entry):
  - has_vat          : VAT amount / totals (not wording)
  - has_irpf         : IRPF amount or withholding_type
  - transaction_type : "income" | "expense"
  - is_rent          : withholding_type == rental
  - is_professional  : withholding_type professional / irpf_work
  - operation_type   : stored tax nature (ISP, intra, recargo, …)

Matching strategy (fully data-driven):
  Each modelo in the modelos collection has a `name` field.
  We tokenise the name and match against the signal set.
  Keyword maps are defined here as configuration — NOT as hardcoded modelo numbers.
  The modelos collection is the single source of truth for what exists.
  The census_data collection is the single source of truth for what applies to a user.
"""

import re
import os
import logging
import certifi
from datetime import datetime
from typing import List, Dict, Optional

from bson import ObjectId
from pymongo import MongoClient
from app.services.fiscal_profile_service import get_canonical_fiscal_profile
from app.services.tax_nature import (
    persistable_nature,
    signals_from_nature,
    apply_tax_nature,
    normalize_operation_type,
    normalize_withholding_type,
)
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Signal → keyword map
# Keys are signal names; values are regex patterns matched against modelo.name.
# Add new signals here as new modelos are added to the system — never hardcode
# a modelo number anywhere in this file.
# ─────────────────────────────────────────────────────────────────────────────
SIGNAL_KEYWORD_MAP: Dict[str, List[str]] = {
    # VAT-related modelos — matches Spanish AND English modelo names
    "has_vat":          [r"iva", r"valor\s+a[ñn]adido", r"impuesto.*valor",
                         r"vat", r"value\s+added", r"quarterly.*vat", r"vat.*return"],
    # IRPF income tax modelos (quarterly)
    "has_irpf":         [r"irpf", r"renta", r"rendimientos", r"pago\s+fraccionado",
                         r"retenci[oó]n.*trabajo", r"retenci[oó]n.*profesional",
                         r"income\s+tax", r"quarterly.*irpf", r"direct\s+estim"],
    # Rent / property modelos (115)
    "is_rent":          [r"alquiler", r"arrendamiento", r"inmueble",
                         r"retenci[oó]n.*arrend",
                         r"rental", r"withhold.*rent", r"rent.*withhold"],
    # Professional services / employee withholding (111)
    "is_professional":  [r"profesional", r"honorarios", r"actividad.*econ[oó]mica",
                         r"trabajo\s+personal", r"rendimientos.*trabajo",
                         r"professional", r"withhold.*professional",
                         r"employee.*withhold", r"work.*withhold"],
    # Income transactions — matches modelos that handle sales/output VAT
    "is_income":        [r"ventas", r"ingresos", r"factura.*emitida", r"repercutido",
                         r"sales", r"output.*vat", r"revenue"],
    # Expense transactions — matches modelos that handle purchases/input VAT
    "is_expense":       [r"compras", r"gastos", r"factura.*recibida", r"soportado",
                         r"purchase", r"input.*vat", r"expense"],
    # Annual summary modelos (390, 190)
    "is_annual":        [r"resumen\s+anual", r"declaraci[oó]n\s+anual",
                         r"annual.*summary", r"annual.*return", r"summary.*annual"],
}


def _extract_signals(entry: dict) -> Dict[str, bool]:
    """
    Legal signals from stored tax nature + amounts.
    Invoice description is not used once operation_type / withholding_type exist.
    """
    invoice_data = entry.get("invoice_data") or {}
    ocr_text = entry.get("ocr_text") or ""
    invoice_data, nature = persistable_nature(invoice_data, ocr_text=ocr_text)
    entry["invoice_data"] = invoice_data
    return signals_from_nature(invoice_data, nature)


def _modelo_matches_signals(
    modelo_name: str, signals: Dict[str, bool]
) -> Optional[str]:
    """
    Check whether a modelo's name matches any active signal.

    Returns an explanation string if matched, None if not.
    Matching is purely keyword-based against the modelo name — no hardcoded numbers.
    """
    name_lower = modelo_name.lower()
    matched_signals = []

    for signal_name, patterns in SIGNAL_KEYWORD_MAP.items():
        if not signals.get(signal_name):
            continue  # signal not active for this entry
        for pattern in patterns:
            if re.search(pattern, name_lower, re.IGNORECASE):
                matched_signals.append(f"{signal_name} → '{pattern}' matched in '{modelo_name}'")
                break  # one match per signal is enough

    if matched_signals:
        return "; ".join(matched_signals)
    return None


class TaxClassificationService:
    """
    Classifies a ledger entry against the user's applicable modelos.

    Data sources (single source of truth):
      - modelos collection    : all known tax models and their names
      - census_data collection: which modelos apply to this specific user
    """

    def __init__(self):
        client = MongoClient(os.getenv("MONGO_URI"), tlsCAFile=certifi.where())
        db = client[os.getenv("DB_NAME")]
        self._modelos     = db["modelos"]
        self._census      = db["census_data"]
        self._users       = db["users"]
        self._ledger      = db["ledger"]

    # ─────────────────── public API ──────────────────────────────────────────

    def classify_ledger_entry(self, ledger_id: str, user_id: str) -> dict:
        """
        Classify a single ledger entry and persist the result back into it.

        Returns the tax_classification dict that was written to the document.
        """
        entry = self._ledger.find_one({"_id": ObjectId(ledger_id)})
        if not entry:
            raise ValueError(f"Ledger entry {ledger_id} not found")

        classification = self._classify(entry, user_id)
        self._persist_classification(entry["_id"], entry.get("invoice_data") or {}, classification)
        logger.info(
            f"[ClassificationLayer] entry={ledger_id} "
            f"modelos={[m['modelo_no'] for m in classification['matched_modelos']]}"
        )
        return classification

    def classify_entry_dict(self, entry: dict, user_id: str) -> dict:
        """
        Classify an in-memory ledger entry dict (no DB write).
        Used by the OCR pipeline immediately after insert.
        """
        return self._classify(entry, user_id)

    def backfill_user(self, user_id: str) -> Dict[str, int]:
        """
        Re-classify ALL ledger entries for a user — including ones that were
        previously classified but got empty modelo_ids (wrong classification).
        """
        stats = {"processed": 0, "classified": 0, "skipped": 0, "errors": 0}
        entries = list(self._ledger.find({
            "user_id": user_id,
            "processing_status": "success",
        }))
        for entry in entries:
            stats["processed"] += 1
            try:
                classification = self._classify(entry, user_id)
                self._persist_classification(
                    entry["_id"], entry.get("invoice_data") or {}, classification
                )
                if classification["matched_modelos"]:
                    stats["classified"] += 1
                else:
                    stats["skipped"] += 1
            except Exception as e:
                logger.error(f"[ClassificationLayer] backfill error entry={entry['_id']}: {e}")
                stats["errors"] += 1
        return stats

    # ─────────────────── internals ───────────────────────────────────────────

    def _classify(self, entry: dict, user_id: str) -> dict:
        """
        Core classification logic.

        Steps:
          1. Get user's applicable modelos from census_data
          2. Load those modelos from the modelos collection
          3. Extract signals from the ledger entry
          4. Match signals against each modelo's name
          5. Return structured result with modelo_ids + explanations
        """
        applicable_nos = self._get_user_applicable_modelo_nos(user_id)
        if not applicable_nos:
            return self._empty_result("No census obligations found for user")

        modelos = self._load_modelos_by_nos(applicable_nos)
        if not modelos:
            return self._empty_result("No matching modelos found in modelos collection")

        signals = _extract_signals(entry)
        auto_matched = []

        for modelo in modelos:
            explanation = _modelo_matches_signals(modelo["name"], signals)
            if explanation:
                auto_matched.append({
                    "modelo_id":  str(modelo["_id"]),
                    "modelo_no":  modelo["modelo_no"],
                    "modelo_name": modelo["name"],
                    "explanation": explanation,
                })

        existing = entry.get("tax_classification") or {}
        override = existing.get("user_override")
        if override and existing.get("matched_modelos"):
            matched = existing["matched_modelos"]
        else:
            matched = auto_matched
            override = False

        return {
            "modelo_ids":      [m["modelo_id"] for m in matched],
            "matched_modelos": matched,
            "auto_matched_modelos": auto_matched,
            "signals":         signals,
            "user_override":   bool(override),
            "classified_at":   datetime.utcnow().isoformat(),
        }

    def override_classification(
        self,
        ledger_id: str,
        user_id: str,
        *,
        modelo_nos: Optional[List[str]] = None,
        operation_type: Optional[str] = None,
        withholding_type: Optional[str] = None,
        clear_override: bool = False,
    ) -> dict:
        """Update tax nature and/or lock which modelos this entry belongs to."""
        entry = self._ledger.find_one({"_id": ObjectId(ledger_id)})
        if not entry:
            raise ValueError(f"Ledger entry {ledger_id} not found")

        invoice_data = dict(entry.get("invoice_data") or {})
        nature_changed = False
        if operation_type is not None:
            normalized = normalize_operation_type(operation_type)
            if not normalized:
                raise ValueError(f"Invalid operation_type: {operation_type}")
            invoice_data = apply_tax_nature(
                invoice_data,
                {
                    "operation_type": normalized,
                    "withholding_type": normalize_withholding_type(
                        invoice_data.get("withholding_type")
                    ) or "none",
                },
            )
            nature_changed = True
        if withholding_type is not None:
            normalized = normalize_withholding_type(withholding_type)
            if not normalized:
                raise ValueError(f"Invalid withholding_type: {withholding_type}")
            invoice_data = apply_tax_nature(
                invoice_data,
                {
                    "operation_type": normalize_operation_type(
                        invoice_data.get("operation_type")
                    ) or "general",
                    "withholding_type": normalized,
                },
            )
            nature_changed = True

        if nature_changed:
            entry["invoice_data"] = invoice_data
            if modelo_nos is None:
                existing = dict(entry.get("tax_classification") or {})
                existing.pop("user_override", None)
                entry["tax_classification"] = existing

        if clear_override:
            existing = dict(entry.get("tax_classification") or {})
            existing.pop("user_override", None)
            entry["tax_classification"] = existing

        if modelo_nos is not None:
            applicable_nos = set(self._get_user_applicable_modelo_nos(user_id))
            wanted = [str(no) for no in modelo_nos if str(no)]
            invalid = [no for no in wanted if no not in applicable_nos]
            if invalid:
                raise ValueError(
                    f"Modelo(s) not applicable for this user: {', '.join(invalid)}"
                )
            modelos = self._load_modelos_by_nos(wanted)
            by_no = {m["modelo_no"]: m for m in modelos}
            matched = []
            for no in wanted:
                modelo = by_no.get(no)
                if not modelo:
                    raise ValueError(f"Modelo {no} not found")
                matched.append({
                    "modelo_id": str(modelo["_id"]),
                    "modelo_no": modelo["modelo_no"],
                    "modelo_name": modelo["name"],
                    "explanation": "User override",
                })
            signals = _extract_signals(entry)
            classification = {
                "modelo_ids": [m["modelo_id"] for m in matched],
                "matched_modelos": matched,
                "auto_matched_modelos": [],
                "signals": signals,
                "user_override": True,
                "overridden_at": datetime.utcnow().isoformat(),
                "classified_at": datetime.utcnow().isoformat(),
            }
            self._persist_classification(entry["_id"], entry.get("invoice_data") or {}, classification)
            return classification

        classification = self._classify(entry, user_id)
        self._persist_classification(entry["_id"], entry.get("invoice_data") or {}, classification)
        return classification

    def _persist_classification(
        self, ledger_oid, invoice_data: dict, classification: dict
    ) -> None:
        self._ledger.update_one(
            {"_id": ledger_oid},
            {"$set": {
                "invoice_data": invoice_data,
                "tax_classification": classification,
                "tax_classified_at": datetime.utcnow(),
            }}
        )

    def _get_user_applicable_modelo_nos(self, user_id: str) -> List[str]:
        """
        Read the user's canonical fiscal profile and return the list of
        modelo numbers from periodic_tax_obligations.
        These are the ONLY modelos valid for this user.
        """
        record = get_canonical_fiscal_profile(
            self._users, self._census, user_id
        )
        if not record:
            return []

        obligations = record.get("periodic_tax_obligations") or []
        nos = [
            ob["modelo"]
            for ob in obligations
            if ob.get("modelo")  # skip entries with null modelo
        ]
        return list(set(nos))  # deduplicate

    def _load_modelos_by_nos(self, modelo_nos: List[str]) -> List[dict]:
        """Fetch full modelo documents for the given modelo numbers."""
        return list(self._modelos.find({"modelo_no": {"$in": modelo_nos}}))

    @staticmethod
    def _empty_result(reason: str) -> dict:
        return {
            "modelo_ids":      [],
            "matched_modelos": [],
            "signals":         {},
            "classified_at":   datetime.utcnow().isoformat(),
            "note":            reason,
        }
