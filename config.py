"""Settings for the Collector Parts Finder manuals reader. All keys come from GitHub secrets."""
import os

SUPABASE_URL = os.environ.get("SUPABASE_URL") or "https://txwdjwgdylyudobrdhfa.supabase.co"
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

MODEL_CLASSIFIER = "claude-haiku-4-5"
USE_BATCH_API = False
CACHE_SYSTEM_PROMPTS = True
PRICES = {"haiku": (1.00, 5.00, 1.25, 0.10), "sonnet": (3.00, 15.00, 3.75, 0.30)}
WEB_SEARCH_PRICE_USD = 0.01
SEARCH_TOOL = "web_search_20250305"
