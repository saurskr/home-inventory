"""Command line: python -m receipt_module IMAGE [--classify-only] [--provider ollama|anthropic] ..."""
import argparse
import json
import os
import sys

from . import (AnthropicProvider, OllamaProvider, ProviderError, ReceiptError, classify_image,
               extract_receipt)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m receipt_module",
                                 description="Classify an image and, if it is a receipt, list its items.")
    ap.add_argument("image", help="path to a PNG, JPEG, WebP or GIF")
    ap.add_argument("--provider", choices=("ollama", "anthropic"), default="ollama")
    ap.add_argument("--model", help="model name (default: qwen2.5vl:7b for ollama, claude-haiku-5-5 for anthropic)")
    ap.add_argument("--url", default=os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
                    help="Ollama address (default: OLLAMA_HOST or http://localhost:11434)")
    ap.add_argument("--classify-only", action="store_true", help="only say receipt / item / other")
    ap.add_argument("--force", action="store_true", help="read it as a receipt even if not detected as one")
    a = ap.parse_args(argv)
    try:
        with open(a.image, "rb") as fh:
            data = fh.read()
    except OSError as e:
        print("Cannot read image: %s" % e, file=sys.stderr)
        return 2
    if a.provider == "anthropic":
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            print("Set ANTHROPIC_API_KEY to use --provider anthropic.", file=sys.stderr)
            return 2
        provider = AnthropicProvider(key, a.model or "claude-haiku-5-5")
    else:
        provider = OllamaProvider(a.url, a.model or "qwen2.5vl:7b")
    try:
        kind = classify_image(provider, data) if not a.force else "receipt"
        out = {"kind": kind}
        if kind == "receipt" and not a.classify_only:
            out["receipt"] = extract_receipt(provider, data)
        print(json.dumps(out, indent=2))
        return 0
    except (ProviderError, ReceiptError) as e:
        print("Error: %s" % e.message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
