"""Shared test setup: make the project importable and use fake keys (no real API calls)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["WHATSAPP_TOKEN"] = "test-token"
os.environ["PHONE_NUMBER_ID"] = "12345"
os.environ["VERIFY_TOKEN"] = "my-verify-token"
os.environ["LLM_API_KEY"] = "test-key"
os.environ["LLM_MODEL"] = "main-model"
os.environ["LLM_FALLBACK_MODEL"] = "fallback-model"
