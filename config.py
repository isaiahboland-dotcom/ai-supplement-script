"""
Settings loader. Reads .env (in the same folder as this file) into the
environment, then exposes a bunch of settings as plain module globals.
A real environment variable beats whatever is in .env.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def load_dotenv(path=None):
    if path is None:
        path = BASE_DIR / ".env"
    if not path.exists():
        return

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()

        # strip matching quotes, otherwise chop off a trailing inline comment
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()

        os.environ.setdefault(key, value)


load_dotenv()


def env(key, default=""):
    return os.environ.get(key, default).strip()


def env_int(key, default):
    val = env(key)
    if val:
        return int(val)
    return default


def env_float(key, default):
    val = env(key)
    if val:
        return float(val)
    return default


# -------------------- LLM --------------------

LLM_PROVIDER = env("LLM_PROVIDER", "lmstudio").lower()   # lmstudio | ollama | openai | claude

default_urls = {
    "lmstudio": "http://localhost:1234/v1",
    "ollama": "http://localhost:11434",
    "openai": "https://api.openai.com/v1",
    "claude": "",   # let the SDK pick its own default
}
LLM_BASE_URL = env("LLM_BASE_URL", default_urls.get(LLM_PROVIDER, "")).rstrip("/")

if LLM_PROVIDER == "claude":
    LLM_MODEL = env("LLM_MODEL", "claude-opus-5")
else:
    LLM_MODEL = env("LLM_MODEL")

LLM_API_KEY = env("LLM_API_KEY")
if not LLM_API_KEY:
    # fall back to the provider's usual env var name
    alt = {"openai": "OPENAI_API_KEY", "claude": "ANTHROPIC_API_KEY"}.get(LLM_PROVIDER)
    if alt:
        LLM_API_KEY = env(alt)

# Local models behave better at a low temperature. The hosted APIs (newer
# OpenAI models, current Claude models) reject a custom temperature, so we
# only send one if you've set it yourself.
if LLM_PROVIDER in ("lmstudio", "ollama"):
    LLM_TEMPERATURE = env_float("LLM_TEMPERATURE", 0.2)
else:
    LLM_TEMPERATURE = env_float("LLM_TEMPERATURE", None)

LLM_TIMEOUT = env_int("LLM_TIMEOUT", 600)
LLM_MAX_TOKENS = env_int("LLM_MAX_TOKENS", 16000)    # Claude requires this
OLLAMA_NUM_CTX = env_int("OLLAMA_NUM_CTX", 32768)    # Ollama's default is too small for research
LLM_EFFORT = env("LLM_EFFORT")                       # Claude only, optional: low | medium | high | xhigh | max
CLAUDE_FALLBACKS = env("CLAUDE_FALLBACKS", "default")  # "default" or "off"


# -------------------- Search --------------------

SEARCH_PROVIDER = env("SEARCH_PROVIDER", "brave").lower()   # brave | searxng
BRAVE_API_KEY = env("BRAVE_API_KEY")
BRAVE_API_URL = env("BRAVE_API_URL", "https://api.search.brave.com/res/v1/web/search")
SEARXNG_URL = env("SEARXNG_URL", "http://localhost:8080").rstrip("/")
SEARCH_RESULTS = env_int("SEARCH_RESULTS", 8)
SEARCH_COUNTRY = env("SEARCH_COUNTRY", "US")
SEARCH_LANG = env("SEARCH_LANG", "en")
FETCH_MAX_CHARS = env_int("FETCH_MAX_CHARS", 8000)


# -------------------- Research loop --------------------

MAX_RESEARCH_STEPS = env_int("MAX_RESEARCH_STEPS", 30)
MIN_SEARCHES = env_int("MIN_SEARCHES", 4)


# -------------------- Output --------------------

OUTPUT_DIR = BASE_DIR / env("OUTPUT_DIR", "output")
EXCEL_FILE = OUTPUT_DIR / env("EXCEL_FILE", "supplements_research.xlsx")
RESEARCH_DIR = OUTPUT_DIR / "research"
LOG_DIR = OUTPUT_DIR / "logs"


# -------------------- Grist (optional) --------------------

GRIST_URL = env("GRIST_URL").rstrip("/")
GRIST_DOC_ID = env("GRIST_DOC_ID")
GRIST_TABLE_ID = env("GRIST_TABLE_ID", "Table1")
GRIST_API_KEY = env("GRIST_API_KEY")
GRIST_ENABLED = bool(GRIST_URL and GRIST_DOC_ID and GRIST_API_KEY)
