"""receipt_module: read receipts (and tell them apart from item photos) with a local or cloud vision model.

Standard library only. See README.md.
"""
from .providers import AnthropicProvider, OllamaProvider, ProviderError
from .receipts import (DEFAULT_CATEGORIES, KINDS, ReceiptError, classify_image, extract_receipt,
                       load_image, normalize_date, normalize_receipt, parse_json_loose, to_money)

__version__ = "1.0.0"
__all__ = ["AnthropicProvider", "OllamaProvider", "ProviderError", "ReceiptError", "DEFAULT_CATEGORIES",
           "KINDS", "classify_image", "extract_receipt", "load_image", "normalize_date",
           "normalize_receipt", "parse_json_loose", "to_money"]
