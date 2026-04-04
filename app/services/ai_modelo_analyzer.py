"""
AI Modelo Analyzer Service
Uses OpenAI to analyze invoices and determine the appropriate Spanish tax Modelo ID
"""

import os
import logging
from typing import Optional, Dict, Any
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


class AIModeloAnalyzer:
    """Service for AI-powered Modelo ID determination"""
    
    def __init__(self):
        self.client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self.model = "gpt-4o-mini"  # Cost-effective model for classification
    
    def analyze_invoice_for_modelo(
        self,
        invoice_data: Dict[str, Any],
        ocr_text: str
    ) -> Dict[str, Any]:
        """
        Analyze invoice data and OCR text to determine the appropriate Spanish tax Modelo ID
        
        Args:
            invoice_data: Structured invoice data from OCR extraction
            ocr_text: Raw OCR text from the invoice
            
        Returns:
            Dict with modelo_id, confidence, and reasoning
        """
        try:
            # Prepare invoice context for AI analysis
            context = self._prepare_invoice_context(invoice_data, ocr_text)
            
            # Call OpenAI to analyze and determine Modelo ID
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": self._get_system_prompt()
                    },
                    {
                        "role": "user",
                        "content": context
                    }
                ],
                temperature=0.1,  # Low temperature for consistent classification
                max_tokens=500
            )
            
            # Parse AI response
            ai_response = response.choices[0].message.content.strip()
            result = self._parse_ai_response(ai_response)
            
            logger.info(f"AI Modelo Analysis: {result['modelo_id']} (confidence: {result['confidence']})")
            return result
            
        except Exception as e:
            logger.error(f"Error in AI Modelo analysis: {e}")
            return {
                "modelo_id": None,
                "confidence": 0.0,
                "reasoning": f"Analysis failed: {str(e)}",
                "error": str(e)
            }
    
    def _get_system_prompt(self) -> str:
        """Get the system prompt for Modelo ID classification"""
        return """You are an expert in Spanish tax law and accounting. Your task is to analyze invoices and determine the appropriate Spanish tax Modelo (tax form) ID.

Spanish Tax Modelos (Common ones):
- Modelo 303: Quarterly VAT (IVA) return - for businesses with VAT transactions
- Modelo 130: Quarterly IRPF (income tax) payment - for self-employed professionals
- Modelo 111: Quarterly withholding tax on employee/professional payments
- Modelo 115: Quarterly withholding tax on rental income
- Modelo 190: Annual summary of withholdings
- Modelo 390: Annual VAT summary
- Modelo 347: Annual declaration of operations with third parties (>3,005.06€)
- Modelo 349: Intra-community operations summary
- Modelo 036/037: Census declaration (registration)

Analysis Guidelines:
1. Look for VAT (IVA) amounts → Modelo 303 (quarterly) or 390 (annual)
2. Look for IRPF/retention keywords → Modelo 130 (self-employed) or 111 (withholdings)
3. Look for rental/lease (alquiler/arrendamiento) → Modelo 115
4. Transaction type (income vs expense) affects which modelo applies
5. Consider the business context and transaction nature

Response Format (JSON):
{
  "modelo_id": "303",
  "confidence": 0.95,
  "reasoning": "Invoice contains VAT (IVA) at 21% rate, indicating a taxable transaction requiring quarterly VAT reporting via Modelo 303."
}

If uncertain, provide your best estimate with lower confidence and explain why."""
    
    def _prepare_invoice_context(self, invoice_data: Dict[str, Any], ocr_text: str) -> str:
        """Prepare invoice context for AI analysis"""
        totals = invoice_data.get("totals", {})
        supplier = invoice_data.get("supplier", {})
        invoice_info = invoice_data.get("invoice", {})
        transaction_type = invoice_data.get("transaction_type", "unknown")
        
        context = f"""Analyze this Spanish invoice and determine the appropriate Modelo ID:

TRANSACTION TYPE: {transaction_type}

INVOICE DETAILS:
- Invoice Number: {invoice_info.get('invoice_number', 'N/A')}
- Invoice Date: {invoice_info.get('invoice_date', 'N/A')}
- Due Date: {invoice_info.get('due_date', 'N/A')}

SUPPLIER:
- Business Name: {supplier.get('business_name', 'N/A')}
- Email: {supplier.get('Email', 'N/A')}

FINANCIAL TOTALS:
- Subtotal: €{totals.get('total', 0)}
- VAT Rate: {totals.get('VAT_rate', 0)}%
- VAT Amount: €{totals.get('VAT_amount', 0)}
- Total with Tax: €{totals.get('Total_with_Tax', 0)}

OCR TEXT EXCERPT (first 500 chars):
{ocr_text[:500]}

Based on this information, determine the most appropriate Spanish tax Modelo ID."""
        
        return context
    
    def _parse_ai_response(self, ai_response: str) -> Dict[str, Any]:
        """Parse AI response to extract modelo_id, confidence, and reasoning"""
        import json
        import re
        
        try:
            # Try to parse as JSON first
            if ai_response.strip().startswith("{"):
                data = json.loads(ai_response)
                return {
                    "modelo_id": data.get("modelo_id"),
                    "confidence": float(data.get("confidence", 0.0)),
                    "reasoning": data.get("reasoning", ""),
                    "error": None
                }
        except json.JSONDecodeError:
            pass
        
        # Fallback: Extract modelo_id using regex
        modelo_match = re.search(r"modelo[_\s]*(?:id)?[:\s]*[\"']?(\d{3})[\"']?", ai_response, re.IGNORECASE)
        confidence_match = re.search(r"confidence[:\s]*(\d+\.?\d*)", ai_response, re.IGNORECASE)
        
        modelo_id = modelo_match.group(1) if modelo_match else None
        confidence = float(confidence_match.group(1)) if confidence_match else 0.5
        
        return {
            "modelo_id": modelo_id,
            "confidence": confidence,
            "reasoning": ai_response,
            "error": None if modelo_id else "Could not parse modelo_id from response"
        }
