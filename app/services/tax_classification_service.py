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
  - has_vat          : VAT amount > 0
  - has_irpf         : "retención" / "irpf" keyword in OCR text
  - transaction_type : "income" | "expense"
  - is_rent          : "alquiler" / "rent" keyword
  - is_professional  : "honorarios" / "servicios profesionales" keyword

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
    Extract boolean financial/semantic signals from a ledger entry.
    These signals are matched against modelo names to determine attribution.
    """
    invoice_data = entry.get("invoice_data") or {}
    totals       = invoice_data.get("totals") or {}
    ocr_text     = (entry.get("ocr_text") or "").lower()
    tx_type      = str(invoice_data.get("transaction_type", "")).lower()

    vat_amount = float(totals.get("VAT_amount") or 0)
    vat_rate   = float(totals.get("VAT_rate") or 0)
    raw_total  = float(totals.get("total") or 0)

    # ── has_vat: three ways to confirm VAT is present ────────────────────────
    # 1. VAT_amount is a real monetary value (not the rate stored as amount)
    monetary_vat = vat_amount > 0 and vat_amount != vat_rate
    # 2. OCR bug: VAT_amount == VAT_rate but the invoice text mentions IVA/VAT
    #    (e.g. "VAT (21%): €210.00" in ocr_text even though totals.VAT_amount=21)
    ocr_mentions_vat = bool(re.search(
        r"\b(iva|vat|igic|impuesto\s+valor)\b", ocr_text, re.IGNORECASE
    ))
    # 3. Total_with_Tax > total (implies VAT was added)
    total_with_tax = float(totals.get("Total_with_Tax") or 0)
    implicit_vat   = total_with_tax > raw_total > 0

    has_vat = monetary_vat or ocr_mentions_vat or implicit_vat

    has_irpf = bool(re.search(
        r"retenci[oó]n|irpf|pago\s+fraccionado", ocr_text, re.IGNORECASE
    ))
    is_rent = bool(re.search(
        r"alquiler|arrendamiento|rent\b|inmueble", ocr_text, re.IGNORECASE
    ))
    is_professional = bool(re.search(
        r"honorarios|servicios?\s+profesionales?|prestaci[oó]n\s+de\s+servicios?"
        r"|freelance|software\s+development|consulting",
        ocr_text, re.IGNORECASE
    ))

    return {
        "has_vat":         has_vat,
        "has_irpf":        has_irpf,
        "is_rent":         is_rent,
        "is_professional": is_professional,
        "is_income":       tx_type in ("income", "credit"),
        "is_expense":      tx_type in ("expense", "debit"),
    }


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
        self._ledger.update_one(
            {"_id": ObjectId(ledger_id)},
            {"$set": {
                "tax_classification": classification,
                "tax_classified_at": datetime.utcnow(),
            }}
        )
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
            # Include: no classification OR classification with empty modelo_ids
            "$or": [
                {"tax_classification": {"$exists": False}},
                {"tax_classification.modelo_ids": {"$size": 0}},
            ]
        }))
        for entry in entries:
            stats["processed"] += 1
            try:
                classification = self._classify(entry, user_id)
                self._ledger.update_one(
                    {"_id": entry["_id"]},
                    {"$set": {
                        "tax_classification": classification,
                        "tax_classified_at": datetime.utcnow(),
                    }}
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
        matched = []

        for modelo in modelos:
            explanation = _modelo_matches_signals(modelo["name"], signals)
            if explanation:
                matched.append({
                    "modelo_id":  str(modelo["_id"]),
                    "modelo_no":  modelo["modelo_no"],
                    "modelo_name": modelo["name"],
                    "explanation": explanation,
                })

        return {
            "modelo_ids":      [m["modelo_id"] for m in matched],
            "matched_modelos": matched,
            "signals":         signals,
            "classified_at":   datetime.utcnow().isoformat(),
        }

    def _get_user_applicable_modelo_nos(self, user_id: str) -> List[str]:
        """
        Read the user's latest census_data record and return the list of
        modelo numbers from periodic_tax_obligations.
        These are the ONLY modelos valid for this user.
        """
        record = self._census.find_one(
            {"user_id": user_id},
            sort=[("created_at", -1)]
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
