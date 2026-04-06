# AI Modelo Analysis Feature

## Overview
This feature adds AI-powered Spanish tax Modelo ID determination to the OCR processing pipeline. When invoices are processed through OCR, the system now automatically analyzes them using OpenAI to determine the appropriate Spanish tax form (Modelo) that applies.

## What's New

### 1. AI Modelo Analyzer Service
**File:** `app/services/ai_modelo_analyzer.py`

A new service that uses OpenAI GPT-4o-mini to analyze invoices and determine the appropriate Modelo ID based on:
- Transaction type (income vs expense)
- VAT (IVA) presence and rates
- IRPF keywords and retention indicators
- Rental/lease indicators
- Professional services indicators
- Invoice financial details

The AI provides:
- `modelo_id`: The determined Spanish tax form number (e.g., "303", "130", "111")
- `confidence`: A confidence score (0.0 to 1.0) indicating how certain the AI is
- `reasoning`: Explanation of why this Modelo was chosen

### 2. Enhanced Ledger Entries
Ledger entries now include three new fields:
- `ai_modelo_id`: The AI-determined Modelo ID
- `ai_modelo_confidence`: Confidence score (0.0 to 1.0)
- `ai_modelo_reasoning`: AI's explanation for the determination

### 3. Updated OCR Pipeline
**File:** `app/routes/ocr.py`

The OCR processing now:
1. Extracts invoice data using regex (existing)
2. Calls AI Modelo Analyzer to determine Modelo ID (new)
3. Stores AI analysis results in ledger entry (new)
4. Proceeds with tax classification and transaction creation (existing)

### 4. Enhanced Tax Calculations
**File:** `app/services/tax_calculation_service.py`

Tax transactions now include:
- `ai_modelo_id`: Linked from ledger entry
- `ai_modelo_confidence`: Confidence score
- `modelo_ids`: Array that includes the AI-determined Modelo ID

This ensures proper tax calculations based on the AI-determined Modelo.

### 5. Frontend Ledger Display
**File:** `contia365_frontend/src/components/pages/app/ledger/index.jsx`

The ledger table now displays:
- **Modelo ID column**: Shows the AI-determined Modelo as a badge
- **Confidence indicator**: Shows confidence percentage next to the Modelo ID
- **CSV Export**: Includes Modelo ID and confidence in exported data

## Spanish Tax Modelos Supported

The AI is trained to recognize these common Spanish tax forms:

- **Modelo 303**: Quarterly VAT (IVA) return
- **Modelo 130**: Quarterly IRPF payment for self-employed
- **Modelo 111**: Quarterly withholding tax on employee/professional payments
- **Modelo 115**: Quarterly withholding tax on rental income
- **Modelo 190**: Annual summary of withholdings
- **Modelo 390**: Annual VAT summary
- **Modelo 347**: Annual declaration of operations with third parties
- **Modelo 349**: Intra-community operations summary

## Configuration

### Required Environment Variable
Ensure your `.env` file includes:
```env
OPENAI_API_KEY=your_openai_api_key
```

### Cost Considerations
- Uses GPT-4o-mini model for cost-effectiveness
- Low temperature (0.1) for consistent results
- Max 500 tokens per analysis
- Estimated cost: ~$0.001 per invoice analysis

## How It Works

### Processing Flow
```
Invoice Upload → OCR Extraction → AI Modelo Analysis → Ledger Entry Creation
                                         ↓
                                  Tax Classification
                                         ↓
                                  Tax Transaction Creation
```

### Example AI Analysis
```json
{
  "modelo_id": "303",
  "confidence": 0.95,
  "reasoning": "Invoice contains VAT (IVA) at 21% rate, indicating a taxable transaction requiring quarterly VAT reporting via Modelo 303."
}
```

## Benefits

1. **Automated Classification**: No manual Modelo selection needed
2. **Accurate Tax Reporting**: Proper Modelo ensures correct tax calculations
3. **Audit Trail**: AI reasoning provides transparency
4. **Confidence Scoring**: Low confidence scores flag entries for review
5. **Spain-Specific**: Trained on Spanish tax law and accounting practices

## Usage

### For Users
1. Upload invoices through the normal OCR process
2. View Modelo ID in the Ledger table
3. Check confidence score to identify uncertain classifications
4. Export ledger data with Modelo information included

### For Developers
```python
from app.services.ai_modelo_analyzer import AIModeloAnalyzer

analyzer = AIModeloAnalyzer()
result = analyzer.analyze_invoice_for_modelo(
    invoice_data=invoice_data,
    ocr_text=ocr_text
)

print(f"Modelo: {result['modelo_id']}")
print(f"Confidence: {result['confidence']}")
print(f"Reasoning: {result['reasoning']}")
```

## Error Handling

The system gracefully handles AI analysis failures:
- If AI analysis fails, ledger entry is still created
- `ai_modelo_id` will be `None` if analysis fails
- Error is logged but doesn't block OCR processing
- Fallback to existing tax classification logic

## Future Enhancements

Potential improvements:
1. Manual Modelo override in UI
2. Confidence threshold alerts
3. Batch re-analysis for existing entries
4. Multi-language support (Catalan, Basque, Galician)
5. Historical accuracy tracking
6. Fine-tuned model for Spanish tax forms

## Testing

To test the feature:
1. Ensure `OPENAI_API_KEY` is configured
2. Upload an invoice through the Execution page
3. Check the Ledger page for the Modelo ID column
4. Verify confidence score is displayed
5. Export CSV to confirm Modelo data is included

## Troubleshooting

### Modelo ID not appearing
- Check OpenAI API key is valid
- Check API quota/billing
- Review logs for AI analysis errors

### Low confidence scores
- Invoice may be ambiguous
- OCR text quality may be poor
- Consider manual review

### Wrong Modelo assigned
- Review AI reasoning in database
- Check if invoice data extraction is accurate
- Report edge cases for system improvement
