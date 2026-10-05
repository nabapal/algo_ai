"""Diagnose plain Gemini generation and optional Google Search grounding.

Run from the project directory with:
    python test_gemini_api.py
    python test_gemini_api.py --grounded --application-prompt

The script reads GEMINI_API_KEY and GEMINI_MODEL through app_config, never
prints the key, and exits non-zero when configuration or the API call fails.
Grounded requests are sent once without an automatic retry so Google's
original grounding error code and message remain visible.
"""

from __future__ import annotations

import argparse
import sys

import requests

from app_config import load_settings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        help="Test this Gemini model instead of GEMINI_MODEL (does not edit .env).",
    )
    parser.add_argument(
        "--list-models",
        action="store_true",
        help="List models available to this API key that support generateContent.",
    )
    parser.add_argument(
        "--grounded",
        action="store_true",
        help="Enable Google Search grounding for this request.",
    )
    parser.add_argument(
        "--application-prompt",
        action="store_true",
        help="Use the app's research prompt instead of the short health-check prompt.",
    )
    args = parser.parse_args()

    settings = load_settings()
    api_key = (settings.get("gemini_api_key") or "").strip()
    model = (args.model or settings.get("gemini_model") or "gemini-flash-latest").strip()
    provider = (settings.get("ai_provider") or "unset").strip()

    print(f"Provider: {provider}; Gemini model: {model}")
    if not api_key:
        print("FAIL: GEMINI_API_KEY was not loaded from the environment or .env.")
        return 2

    if args.list_models:
        try:
            response = requests.get(
                "https://generativelanguage.googleapis.com/v1beta/models",
                headers={"x-goog-api-key": api_key},
                timeout=25,
            )
        except requests.RequestException as exc:
            print(f"FAIL: Model list request failed ({type(exc).__name__}: {exc}).")
            return 1
        print(f"HTTP status: {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not response.ok:
            message = payload.get("error", {}).get("message")
            print(f"FAIL: {message or 'Could not list Gemini models.'}")
            return 1
        available = [
            item.get("name", "").removeprefix("models/")
            for item in payload.get("models", [])
            if "generateContent" in item.get("supportedGenerationMethods", [])
        ]
        if not available:
            print("No generateContent models were returned for this API key.")
            return 1
        print("Models supporting generateContent for this key:")
        for name in available:
            print(f"  {name}")
        return 0

    endpoint = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )
    if args.application_prompt:
        from ai_sentiment import _fixed_prompt

        prompt = _fixed_prompt(index=settings.get("index", "NIFTY"))
    elif args.grounded:
        prompt = (
            'Use Google Search to find a current market-news headline. '
            'Reply with exactly {"status":"ok"}.'
        )
    else:
        prompt = 'Reply with exactly {"status":"ok"}'

    request_body = {"contents": [{"parts": [{"text": prompt}]}]}
    if args.grounded:
        request_body["tools"] = [{"google_search": {}}]
    print(f"Google Search grounding: {'enabled' if args.grounded else 'disabled'}")
    print(f"Prompt mode: {'application' if args.application_prompt else 'short health check'}")

    try:
        response = requests.post(
            endpoint,
            headers={
                "x-goog-api-key": api_key,
                "Content-Type": "application/json",
            },
            json=request_body,
            timeout=25,
        )
    except requests.RequestException as exc:
        print(f"FAIL: Gemini request could not complete ({type(exc).__name__}: {exc}).")
        return 1

    print(f"HTTP status: {response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        payload = {}

    if not response.ok:
        error = payload.get("error", {})
        code = error.get("code", "unknown")
        status = error.get("status", "unknown")
        message = error.get("message", "Gemini returned an unsuccessful response.")
        print(f"FAIL: API code={code}; status={status}; message={message}")
        return 1

    candidates = payload.get("candidates") or []
    parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
    answer = "".join(
        part.get("text", "") for part in parts if isinstance(part, dict)
    ).strip()
    if not answer:
        print("FAIL: Gemini returned HTTP success but no candidate text.")
        return 1

    grounding_metadata = candidates[0].get("groundingMetadata") or {}
    print(f"Google Search grounding metadata returned: {'yes' if grounding_metadata else 'no'}")
    print(f"PASS: Gemini responded: {answer[:300]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
