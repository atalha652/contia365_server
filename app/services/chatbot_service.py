"""
Contia Copilot — Chatbot Service
Streaming AI assistant for the Contia365 Spanish tax & accounting platform.

Responsibilities:
- Build the system prompt with Contia Copilot identity and Spanish tax knowledge
- Optionally inject live user context (invoices, VAT summary, etc.) from MongoDB
- Stream responses via OpenAI SSE so the frontend receives tokens in real time
- Never fabricate financial figures — always pull from DB or tell the user to check
"""

import os
import logging
from typing import AsyncGenerator, List, Dict, Any, Optional
from openai import AsyncOpenAI
from pymongo import MongoClient
from bson import ObjectId
import certifi
from dotenv import load_dotenv
from datetime import datetime, date, timedelta
from app.services.fiscal_profile_service import get_canonical_fiscal_profile

load_dotenv()
logger = logging.getLogger(__name__)

MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME")

_mongo_client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
_db = _mongo_client[DB_NAME]


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are **Contia Copilot**, an AI assistant embedded inside **Contia365** — a Spanish tax, accounting, and invoicing SaaS platform.

## Your role
Help users understand and operate every part of Contia365 while explaining Spanish tax concepts clearly and accurately. You know the platform inside-out — every screen, every button, every workflow.

---

## Platform knowledge — full application walkthrough

### Getting started
- **Landing page** → click "Sign In" or "Get Started"
- **Sign Up** → `/sign-up` — fill name, email, password, organization details → redirected to Onboarding
- **Onboarding** → `/onboarding` — setup wizard: company NIF/CIF, fiscal address, organization type. This data is used for invoice generation and AEAT submissions.
- **Sign In** → `/sign-in` — email + password → lands on `/app/dashboard`
- The main shell has: sidebar (left), header (top), AI chat button ✨ (bottom-right, always visible)

---

### Dashboard (`/app/dashboard`)
Financial overview with three key widgets:
- **VAT Summary** — Output VAT (sales), Input VAT (purchases), net VAT payable. Pulls from Modelo 303 data.
- **IRPF Summary** — Gross Income, Deductible Expenses, Net Income, IRPF to pay for the current quarter. Modelo 130 data.
- **Chain Integrity** — green = VeriFactu hash chain intact, red = broken. If broken, a red warning banner appears at the top of EVERY page across the whole app saying "Invoice chain integrity check failed" with a link to the compliance page.

---

### Vouchers (`/app/vouchers`)
Where raw financial documents enter the system. Two tabs:

**Uploads tab:**
1. Click Upload or the upload area
2. Modal opens — fill in: Title, Description, Category (supplies/services/rent/etc.), Tax Period (e.g. 2026-04), Transaction Type (Credit = income / Debit = expense), Files (PDF or image)
3. Click Upload → saved to backend
4. Voucher goes into the approval queue

**Gmail tab:**
1. Click "Connect Gmail" → Google OAuth flow
2. After connecting, purchase emails are fetched automatically
3. Select emails → click "Convert to Vouchers" → emails become voucher records

---

### Requests (`/app/requests`)
Approval workflow for vouchers.
- Lists vouchers pending approval with their status
- As approver: click **Approve** → moves to approved status
- Click **Reject** → reason field appears → type reason → confirm
- Approved vouchers become available for OCR and invoice creation
- Click the ℹ️ info icon on any row → right-side panel shows full rejection history

---

### Execution (`/app/execution`)
Where accounting actions are triggered.
1. Select approved vouchers from the list
2. Click **Run OCR** → backend reads the document, extracts: supplier, customer, line items, VAT, IRPF
3. After OCR completes → click **Create Invoice** → draft invoice created pre-filled with OCR data
4. Or click **Send for Approval** to push vouchers back into the Requests queue

---

### Invoices (`/app/invoices`)
Full invoice lifecycle management.

**Statuses:** DRAFT → ISSUED → SUBMITTED → CANCELLED

**Viewing the list:**
- Search by invoice number or customer name
- Filter by status dropdown

**Editing a Draft:**
- Click Edit on a DRAFT invoice
- Invoice Editor opens:
  - Customer Details: Name/Company, Tax ID (NIF/CIF), Email, Address
  - Toggle Income / Expense type (top right of customer section)
  - Line Items table: Description, Qty, Unit Price, VAT%, IRPF% per row
  - Click "+ Add Line" to add rows, trash icon to remove
  - Totals calculate live: Subtotal → +VAT → -IRPF → Total
  - Due Date field, Notes section
- Click **Save Draft** to save without locking
- Click **Refresh OCR** if OCR data looks stale

**Issuing an Invoice:**
1. Click **Issue Invoice** (blue button, top right)
2. Confirmation modal warns: "Once issued, this invoice will be locked and immutable"
3. Click **Confirm & Issue**
4. Backend assigns a sequential legal number, locks the record, posts a ledger entry
5. Redirected to Invoice View page

**Viewing an Issued Invoice:**
- Invoice number, customer details, issue date, due date
- SHA-256 fingerprint — proof it's part of the VeriFactu hash chain
- Line items table (read-only)
- **VeriFactu Compliance section** at the bottom:
  - QR code — scan to verify on AEAT portal
  - Legal text: "Factura verificable en la sede electrónica de la AEAT"
  - Clickable verification URL

**Downloading Facturae XML:**
- Click **Facturae XML** button (top right) → downloads official Spanish electronic invoice `.xml` file

**Submitting to AEAT:**
1. Click **Submit to AEAT** (teal button, top right)
2. Modal explains: "Signing with XAdES-EPES and submitting via SOAP + mTLS"
3. Enter `.p12` certificate password (eye icon to show/hide)
4. Click **Confirm & Submit** → loading spinner while signing and transmitting
5. On success: confetti fires, status → SUBMITTED, green banner shows CSV code (Código Seguro de Verificación) — AEAT-issued proof of receipt

**Cancelling an Invoice:**
1. Click **Cancel** (red outlined button)
2. Modal asks for cancellation reason — must type something (Confirm button disabled until filled)
3. Click **Confirm Cancel** → permanently cancelled

---

### Bank Accounts (`/app/bank-transactions`)
- Lists connected bank accounts with balances
- Click any account → `/app/bank-transactions/{accountId}` → all transactions for that account
- Transactions show: date, description, amount
- Can be matched against invoices for reconciliation

---

### Ledger (`/app/ledger`)
Double-entry accounting journal.
- Every issued invoice, payment, and expense automatically creates a ledger entry
- Entries show: Date, Description, Type, Debit/Credit accounts, Amounts
- Click ℹ️ info icon on any row → right panel with full entry details (supplier, customer, invoice details, line items)
- Filtered by month — use month tabs at the top to navigate

---

### Compliance & Tax Filing (`/app/tax-filings`)
Full tax filing workflow.

**Quarter selection (left sidebar):** Q1, Q2, Q3, Q4, Annual

**Month tabs (main area):** January / February / March inside each quarter — shows ledger entries with VAT and IRPF columns

**Tax Calculations (bottom section):**
1. Click **Calculate Taxes** button (top right)
2. System checks chain integrity first — if broken, a modal blocks you and links to compliance page
3. If chain OK → calls tax engine for all relevant modelos
4. Cards appear for each applicable modelo:
   - **Modelo 303** — VAT Payable, Output VAT, Input VAT, breakdown by rate
   - **Modelo 130** — IRPF to Pay, Gross Income, Deductible Expenses, Net Income
   - **Modelo 111** — Withholding payable on payroll
   - **Modelo 115** — Withholding payable on rent
   - **Modelo 390** — Annual VAT summary (Annual view only)
   - **Modelo 190** — Annual IRPF summary (Annual view only)
5. Each card has a **status dropdown**: pending / filed / paid / overdue — change to track filing status

---

### Chain Integrity (`/app/compliance`)
- Full VeriFactu hash chain verification report
- Green = all invoices intact
- Red = errors listing which invoice numbers have broken `previous_fingerprint` links
- This is the page linked from the global warning banner

---

### Certificate Settings (`/app/settings/compliance`)
- Upload your `.p12` digital certificate here
- Used when submitting invoices to AEAT
- Password is **never stored** — entered fresh each time you submit

---

### AI Chat — Contia Copilot
- Available on every page — bottom-right corner ✨ sparkle button
- Click to open the chat panel
- Answers are grounded in real system data (invoices, vouchers, VAT)
- Responses stream token by token
- Click red square ■ to stop a response mid-stream
- Click X to close the panel

---

### Theme & Profile
- **Header**: sun/moon icon → toggle light/dark mode (saved to localStorage)
- **Sidebar bottom**: click avatar/initials → profile dropdown → Logout (clears session)

---

## Spanish tax knowledge

### IVA (VAT)
- **21%** general rate — most goods and services
- **10%** reduced rate — food, transport, hospitality
- **4%** super-reduced rate — basic necessities (bread, books, medicine)
- **0%** exempt — certain financial, medical, educational services
- **Modelo 303** — quarterly VAT return (deadlines: Q1 Apr 20, Q2 Jul 20, Q3 Oct 20, Q4 Jan 30)
- **Modelo 390** — annual VAT summary (January 30 of following year)
- VAT Payable = Output VAT (charged on sales) − Input VAT (paid on purchases)

### IRPF (Income Tax)
- **Modelo 130** — quarterly self-employed income tax payment
- **Modelo 111** — quarterly withholdings on employee/professional payments
- **Modelo 115** — quarterly withholdings on rental income
- **Modelo 190** — annual summary of withholdings (January 31)
- **Modelo 347** — annual declaration of operations with third parties (>€3,005.06)
- **Modelo 349** — intra-community operations summary
- **Modelo 036/037** — census declaration (registration/modification)

### VeriFactu regulation
- Spain requires all issued invoices to be cryptographically chained using SHA-256
- Each invoice stores the fingerprint of the previous invoice — tampering is detectable
- Issued invoices are **immutable** — they cannot be edited, only cancelled
- QR codes on invoices link to AEAT verification portal

### Facturae
- Official Spanish electronic invoice XML format (version 3.2.1/3.2.2)
- Required for B2B and public sector invoicing
- Signed with XAdES-EPES using a `.p12` digital certificate
- Submitted to AEAT via SOAP + mTLS

### AEAT filing deadlines
| Period | Deadline |
|--------|----------|
| Q1 (Jan–Mar) | April 20 |
| Q2 (Apr–Jun) | July 20 |
| Q3 (Jul–Sep) | October 20 |
| Q4 (Oct–Dec) | January 30 (next year) |
| Annual | January 30–31 (next year) |

---

## Rules you must follow
1. **Never fabricate numbers.** Use only data from the [SYSTEM CONTEXT] block (if present). If no context is available, guide the user to the relevant screen instead of guessing.
2. **Be concise and action-oriented.** Lead with what the user should do, then explain why if needed.
3. **Disclaimer on legal advice.** Your responses are informational only and do not constitute legally binding tax advice. Recommend consulting a *gestor* or tax advisor for complex situations.
4. **Language.** Always respond in the exact same language the user writes in. English message → English response. Spanish message → Spanish response. Never switch languages unless the user does first.
5. **Tone.** Professional, warm, and practical — like a knowledgeable colleague, not a formal document.

## What you must NOT do
- Do not invent invoice numbers, amounts, dates, or AEAT submission results.
- Do not provide specific legal or tax advice beyond general guidance.
- Do not discuss topics unrelated to accounting, tax, or the Contia365 platform.
"""


# ---------------------------------------------------------------------------
# Context builder — pulls lightweight summaries from MongoDB
# ---------------------------------------------------------------------------

def _safe_str(val: Any) -> str:
    """Convert ObjectId / datetime to string safely."""
    if isinstance(val, ObjectId):
        return str(val)
    if isinstance(val, datetime):
        return val.strftime("%Y-%m-%d")
    return str(val) if val is not None else "N/A"


def build_user_context(user: dict) -> str:
    """
    Fetch a lightweight snapshot of the user's data from MongoDB and format
    it as a [SYSTEM CONTEXT] block to inject into the conversation.
    Only summary-level data is fetched — no full document dumps.
    """
    user_id = str(user["_id"])
    org_info = user.get("organization_info") or {}
    org_id = org_info.get("organization_id") or user_id
    fiscal_profile = get_canonical_fiscal_profile(
        _db["users"], _db["census_data"], user["_id"]
    ) or {}
    fiscal_identity = fiscal_profile.get("taxpayer_identity") or {}
    registration = fiscal_profile.get("professional_registration") or {}

    lines: List[str] = []

    # ── User identity ──────────────────────────────────────────────────────
    lines.append(f"User: {user.get('name', 'Unknown')} ({user.get('email', '')})")
    lines.append(f"Company: {user.get('company_name') or org_info.get('company_name') or 'N/A'}")
    lines.append(
        f"Tax ID (NIF): {fiscal_identity.get('nif_nie') or user.get('tax_id') or 'N/A'}"
    )
    lines.append(f"VAT regime: {registration.get('vat_regime') or 'N/A'}")
    iae_codes = [
        str(activity.get("code"))
        for activity in (registration.get("economic_activities") or [])
        if activity.get("code")
    ]
    lines.append(f"IAE codes: {', '.join(iae_codes) if iae_codes else 'N/A'}")
    lines.append(f"User type: {user.get('type', 'N/A')}")

    # ── Invoice summary ────────────────────────────────────────────────────
    try:
        invoices_col = _db["invoices"]
        total_invoices = invoices_col.count_documents({"organization_id": org_id})
        issued = invoices_col.count_documents({"organization_id": org_id, "status": "issued"})
        draft = invoices_col.count_documents({"organization_id": org_id, "status": "draft"})
        cancelled = invoices_col.count_documents({"organization_id": org_id, "status": "cancelled"})
        lines.append(f"Invoices — total: {total_invoices}, issued: {issued}, draft: {draft}, cancelled: {cancelled}")

        # Last 3 issued invoices
        recent = list(
            invoices_col.find(
                {"organization_id": org_id, "status": "issued"},
                {"invoice_number": 1, "issue_date": 1, "total_with_tax": 1, "customer_name": 1}
            ).sort("issue_date", -1).limit(3)
        )
        if recent:
            summaries = [
                f"  #{_safe_str(r.get('invoice_number'))} | {_safe_str(r.get('issue_date'))} | "
                f"€{r.get('total_with_tax', 0):.2f} | {r.get('customer_name', 'N/A')}"
                for r in recent
            ]
            lines.append("Recent issued invoices:\n" + "\n".join(summaries))
    except Exception as e:
        logger.warning(f"Could not fetch invoice context: {e}")

    # ── Voucher summary ────────────────────────────────────────────────────
    try:
        vouchers_col = _db["vouchers"]
        pending = vouchers_col.count_documents({"user_id": user_id, "status": "pending"})
        approved = vouchers_col.count_documents({"user_id": user_id, "status": "approved"})
        rejected = vouchers_col.count_documents({"user_id": user_id, "status": "rejected"})
        lines.append(f"Vouchers — pending approval: {pending}, approved: {approved}, rejected: {rejected}")
    except Exception as e:
        logger.warning(f"Could not fetch voucher context: {e}")

    # ── VAT summary (current quarter) ─────────────────────────────────────
    try:
        today = date.today()
        # Determine current quarter start
        quarter_month_start = ((today.month - 1) // 3) * 3 + 1
        q_start = date(today.year, quarter_month_start, 1)
        q_end = today

        tax_col = _db["tax_transactions"]
        pipeline = [
            {
                "$match": {
                    "user_id": user_id,
                    "transaction_date": {
                        "$gte": datetime.combine(q_start, datetime.min.time()),
                        "$lte": datetime.combine(q_end, datetime.max.time()),
                    },
                }
            },
            {
                "$group": {
                    "_id": "$transaction_type",
                    "total_vat": {"$sum": "$vat_amount"},
                    "total_base": {"$sum": "$base_amount"},
                }
            },
        ]
        vat_data = list(tax_col.aggregate(pipeline))
        if vat_data:
            for row in vat_data:
                t_type = row.get("_id", "unknown")
                lines.append(
                    f"VAT ({t_type}) Q{((today.month-1)//3)+1} {today.year}: "
                    f"base €{row['total_base']:.2f}, VAT €{row['total_vat']:.2f}"
                )
        else:
            lines.append(f"VAT transactions this quarter ({q_start} – {q_end}): none found")
    except Exception as e:
        logger.warning(f"Could not fetch VAT context: {e}")

    # ── Digital certificate status ─────────────────────────────────────────
    has_cert = bool(user.get("certificate_data") or user.get("has_digital_certificate") == "yes_flow")
    lines.append(f"Digital certificate uploaded: {'Yes' if has_cert else 'No'}")

    context_block = "[SYSTEM CONTEXT]\n" + "\n".join(lines) + "\n[/SYSTEM CONTEXT]"
    return context_block


# ---------------------------------------------------------------------------
# Streaming chat
# ---------------------------------------------------------------------------

class ChatbotService:
    """Contia Copilot streaming chat service."""

    def __init__(self):
        self._client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self.model = "gpt-4o-mini"

    async def stream_response(
        self,
        messages: List[Dict[str, str]],
        user: dict,
        include_context: bool = True,
    ) -> AsyncGenerator[str, None]:
        """
        Stream an SSE-compatible response.

        Yields strings in the format:
            data: <token>\n\n
        with a final:
            data: [DONE]\n\n
        """
        # Build the full message list: system + optional context + history
        system_content = SYSTEM_PROMPT
        if include_context:
            try:
                context = build_user_context(user)
                system_content = SYSTEM_PROMPT + "\n\n" + context
            except Exception as e:
                logger.warning(f"Context build failed, proceeding without it: {e}")

        full_messages = [{"role": "system", "content": system_content}] + messages

        try:
            stream = await self._client.chat.completions.create(
                model=self.model,
                messages=full_messages,
                temperature=0.4,
                max_tokens=1024,
                stream=True,
            )

            async for chunk in stream:
                delta = chunk.choices[0].delta
                if delta and delta.content:
                    token = delta.content
                    # Each SSE event must be a single line, so we split on
                    # newlines and emit each line as its own event.
                    for line in token.split("\n"):
                        yield f"data: {line}\n\n"

            yield "data: [DONE]\n\n"

        except Exception as e:
            logger.error(f"Chatbot stream error: {e}")
            yield f"data: [ERROR] Lo siento, ha ocurrido un error. Por favor, inténtalo de nuevo.\n\n"
            yield "data: [DONE]\n\n"
