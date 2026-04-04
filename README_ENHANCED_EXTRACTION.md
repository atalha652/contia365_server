# Enhanced Invoice Data Extraction

## Overview
This enhancement improves the invoice data extraction process to eliminate "N/A" values in the ledger by implementing intelligent fallback strategies and system-generated values.

## What Was Improved

### 1. Invoice Number Extraction
**Before:** Showed "N/A" when invoice number couldn't be extracted

**After:** 
- Enhanced regex patterns to catch more invoice number formats (Spanish and English)
- Validates extracted numbers to filter out noise words like "no", "num", "number"
- **System-generated fallback**: Creates unique invoice numbers in format `INV-YYYYMMDD-XXXXXX-N`
  - `YYYYMMDD`: Current date
  - `XXXXXX`: Last 6 characters of voucher ID (for uniqueness)
  - `N`: File index within voucher

**Example:** `INV-20260404-26CD3F-0`

### 2. Supplier Name Extraction
**Before:** Showed "N/A" when supplier couldn't be identified

**After:** Multi-strategy extraction with fallbacks:

**Strategy 1:** Explicit patterns
- Looks for: "from:", "supplier:", "vendor:", "issued by:", "company:", "proveedor:", "de:"
- Cleans up noise words (invoice, factura, bill, receipt)

**Strategy 2:** All-caps company names
- Finds company names in ALL CAPS at document start
- Common in invoice headers

**Strategy 3:** Email domain extraction
- Extracts company name from email domain
- Example: `support@acmecorp.com` → "Acmecorp (from email)"

**Strategy 4:** Capitalized names
- Looks for proper nouns in first 200 characters
- Catches names like "John Smith Company"

**Final Fallback:** "Unknown Supplier"

### 3. Customer Name Extraction
**Before:** Showed "N/A" for most invoices

**After:** Context-aware extraction with fallbacks:

**Strategy 1:** Explicit patterns
- Looks for: "bill to:", "invoice to:", "sold to:", "customer:", "cliente:", "para:"
- Cleans up noise words

**Strategy 2:** Income transaction patterns
- For income invoices, looks for "to:" patterns
- Identifies who you're billing

**Smart Fallbacks:**
- Income transactions: "Customer (Not Specified)"
- Expense transactions: "Self/Company" (you're the customer)

## Technical Implementation

### New Service
**File:** `app/services/invoice_extractor.py`

Contains three main functions:
1. `generate_invoice_number()` - Creates unique system invoice numbers
2. `extract_supplier_name()` - Multi-strategy supplier extraction
3. `extract_customer_name()` - Context-aware customer extraction
4. `extract_invoice_data_enhanced()` - Main extraction function with all enhancements

### Integration
**File:** `app/routes/ocr.py`

Updated to use `extract_invoice_data_enhanced()` instead of the old `extract_invoice_data()`:
- Passes `voucher_id` for unique invoice number generation
- Passes `file_index` for multi-file vouchers
- All existing functionality preserved

## Benefits

1. **No More N/A Values**: Every ledger entry has meaningful data
2. **Better Audit Trail**: System-generated invoice numbers are traceable
3. **Improved UX**: Users see actual names instead of "N/A"
4. **Graceful Degradation**: Always provides a fallback value
5. **Spanish Support**: Handles Spanish invoice formats (factura, proveedor, cliente)

## Examples

### Before Enhancement
```json
{
  "invoice_number": "N/A",
  "supplier": {"business_name": "N/A"},
  "customer": {"company_name": "N/A"}
}
```

### After Enhancement
```json
{
  "invoice_number": "INV-20260404-26CD3F-0",
  "supplier": {"business_name": "Acme Corporation"},
  "customer": {"company_name": "Self/Company"}
}
```

## Extraction Patterns

### Invoice Number Patterns
- English: `invoice no:`, `invoice #`, `inv:`, `order no:`
- Spanish: `número:`, `factura no:`, `factura núm:`

### Supplier Patterns
- English: `from:`, `supplier:`, `vendor:`, `issued by:`, `company:`
- Spanish: `proveedor:`, `de:`

### Customer Patterns
- English: `bill to:`, `invoice to:`, `sold to:`, `customer:`
- Spanish: `cliente:`, `para:`

## Future Enhancements

Potential improvements:
1. Machine learning-based name extraction
2. Company name database lookup
3. Historical data learning (remember previous suppliers/customers)
4. Multi-language support (Catalan, Basque, Galician)
5. Address-based company identification
6. Tax ID (NIF/CIF) extraction for validation

## Testing

To verify the enhancement:
1. Upload an invoice with missing invoice number
2. Check ledger - should show system-generated number like `INV-20260404-XXXXXX-0`
3. Upload invoice with unclear supplier
4. Check ledger - should show extracted or fallback supplier name
5. Verify no "N/A" values appear in Invoice #, Supplier, or Customer columns

## Backward Compatibility

- Existing `extract_invoice_data()` function remains in code (not removed)
- All existing invoice data structures unchanged
- No database migration required
- Works with existing ledger display code
