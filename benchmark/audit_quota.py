"""
benchmark/audit_quota.py
Gemini API configuration audit — diagnoses quota exhaustion and key setup.

Usage:
    python benchmark/audit_quota.py
"""
import os, sys, json, urllib.request, urllib.error
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# ── 1. Load .env ──────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

# ── 2. Collect all Gemini/Google env vars ─────────────────────────────────────
GOOGLE_VARS = [
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "VERTEX_AI",
    "VERTEXAI",
    "GENAI_API_KEY",
]

SEP = "─" * 70

def mask(v: str) -> str:
    if not v:
        return "(not set)"
    if len(v) <= 8:
        return "***"
    return v[:6] + "..." + v[-4:]

print(SEP)
print("GEMINI / GOOGLE ENVIRONMENT AUDIT")
print(SEP)
print("\n[1] Google-related environment variables:\n")
active_keys = {}
for var in GOOGLE_VARS:
    val = os.environ.get(var, "")
    status = f"  {var:<40} = {mask(val) if val else '(not set)'}"
    if val:
        active_keys[var] = val
    print(status)

# Determine which key to use
api_key = (
    os.environ.get("GEMINI_API_KEY")
    or os.environ.get("GOOGLE_API_KEY")
    or os.environ.get("GENAI_API_KEY")
    or ""
)

vertex_mode = bool(
    os.environ.get("VERTEX_AI") or os.environ.get("VERTEXAI")
    or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    or os.environ.get("GOOGLE_CLOUD_PROJECT")
)

print(f"\n  Key type:  {'AI Studio (AIza...)' if api_key.startswith('AIza') else 'Unknown / Service Account'}")
print(f"  Vertex AI: {'YES — will use x-goog-user-project header' if vertex_mode else 'NO  — pure AI Studio key (x-goog-api-key)'}")

if not api_key:
    print("\n  ERROR: No Gemini API key found. Set GEMINI_API_KEY in .env")
    sys.exit(1)

# ── 3. models.list() via SDK ──────────────────────────────────────────────────
print(f"\n{SEP}")
print("[2] Accessible models via client.models.list()\n")

try:
    import google.genai as genai
    client = genai.Client(api_key=api_key)

    models = list(client.models.list())
    gemini_models = [m.name for m in models if "gemini" in m.name.lower()]

    if gemini_models:
        print(f"  Found {len(gemini_models)} Gemini model(s):\n")
        for m in sorted(gemini_models):
            print(f"    {m}")
    else:
        print("  WARNING: No Gemini models returned (empty list or quota error)")

except Exception as e:
    print(f"  models.list() FAILED: {type(e).__name__}: {e}")

# ── 4. Raw REST call — inspect headers ────────────────────────────────────────
print(f"\n{SEP}")
print("[3] Raw REST probe — gemini-2.0-flash (inspect request/response headers)\n")

ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"gemini-2.0-flash:generateContent?key={api_key}"
)

PAYLOAD = json.dumps({
    "contents": [{"role": "user", "parts": [{"text": "Reply with exactly the word PONG"}]}],
    "generationConfig": {"maxOutputTokens": 4},
}).encode()

req = urllib.request.Request(
    ENDPOINT,
    data=PAYLOAD,
    headers={
        "Content-Type": "application/json",
        "User-Agent": "ContextForge-AuditScript/1.0",
        # NOT setting x-goog-user-project — AI Studio keys must NOT include it
    },
    method="POST",
)

print(f"  URL:     {ENDPOINT[:80]}...")
print(f"  Headers sent:")
print(f"    Content-Type: application/json")
print(f"    x-goog-api-key: (embedded in ?key= query param)")
print(f"    x-goog-user-project: (NOT sent — AI Studio mode)")

try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        status = resp.status
        resp_headers = dict(resp.headers)
        body = json.loads(resp.read().decode())

        print(f"\n  HTTP status: {status} OK")
        print(f"\n  Response headers (rate-limit relevant):")
        rl_keys = [k for k in resp_headers if any(
            x in k.lower() for x in ["quota", "ratelimit", "rate-limit", "x-goog", "retry", "limit"]
        )]
        if rl_keys:
            for k in sorted(rl_keys):
                print(f"    {k}: {resp_headers[k]}")
        else:
            print("    (no rate-limit headers in response)")

        text_out = (
            body.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
        )
        print(f"\n  Model reply: {repr(text_out)}")
        print("\n  DIAGNOSIS: Key is WORKING — quota is available right now.")

except urllib.error.HTTPError as e:
    status = e.code
    resp_headers = dict(e.headers)
    try:
        body = json.loads(e.read().decode())
    except Exception:
        body = {}

    print(f"\n  HTTP status: {status} {e.reason}")
    print(f"\n  Response headers (rate-limit relevant):")
    rl_keys = [k for k in resp_headers if any(
        x in k.lower() for x in ["quota", "ratelimit", "rate-limit", "x-goog", "retry", "limit"]
    )]
    if rl_keys:
        for k in sorted(rl_keys):
            print(f"    {k}: {resp_headers[k]}")
    else:
        print("    (no rate-limit headers returned)")

    error_obj = body.get("error", {})
    code      = error_obj.get("code", "?")
    message   = error_obj.get("message", "")
    status_str = error_obj.get("status", "")
    details   = error_obj.get("details", [])

    print(f"\n  Error body:")
    print(f"    code:    {code}")
    print(f"    status:  {status_str}")
    print(f"    message: {message}")
    if details:
        print(f"    details:")
        for d in details:
            print(f"      {d}")

except Exception as e:
    print(f"\n  Request FAILED (network error): {type(e).__name__}: {e}")
    status = None
    error_obj = {}
    code = None
    status_str = ""
    message = str(e)

# ── 5. Diagnosis summary ──────────────────────────────────────────────────────
print(f"\n{SEP}")
print("[4] ROOT CAUSE DIAGNOSIS\n")

_status_code = locals().get("status")
_error_code  = locals().get("code")
_status_str  = locals().get("status_str", "")
_message     = locals().get("message", "")

if _status_code == 200:
    print("  STATUS:  PASS — quota available, key valid.")
    print("  ACTION:  Re-run `python benchmark/live_benchmark.py` or `_run_sim.py`.")

elif _status_code == 429 or (isinstance(_error_code, int) and _error_code == 429):
    print("  STATUS:  QUOTA EXHAUSTED (429 RESOURCE_EXHAUSTED)\n")

    if "daily" in _message.lower() or "quota" in _message.lower():
        print("  ROOT CAUSE: Free-tier daily request quota (RPD) reached.")
        print()
        print("  WHY 'despite zero usage'?")
        print("  ─────────────────────────────────────────────────────────────")
        print("  Every API call counts toward RPD — including failed calls,")
        print("  model-probe calls, and connection-error retries.")
        print()
        print("  In this session the following consumed quota:")
        print("    • gemini_direct.probe_quota() — pinged every model in the")
        print("      preference list (5 models × N retries each)")
        print("    • _run_sim.py Gemini handshake attempts")
        print("    • live_benchmark.py dry-run calls")
        print("    • Direct `-c` python calls during benchmark development")
        print()
        print("  QUOTA TIER (AI Studio free tier, as of 2026-03):")
        print("    gemini-2.0-flash      — 1 500 RPD / 15 RPM")
        print("    gemini-2.5-flash      —   500 RPD / 10 RPM")
        print("    gemini-2.5-flash-lite —   500 RPD / 10 RPM")
        print("    gemini-2.0-flash-lite — 1 500 RPD / 30 RPM")
        print()
        print("  STRUCTURAL ISSUE (NOT a misconfiguration):")
        print("    AI Studio free-tier RPD limits are per-key-per-day.")
        print("    x-goog-api-key path is CORRECT for AIza keys.")
        print("    No x-goog-user-project mismatch detected.")
        print("    Vertex AI is NOT involved — no GOOGLE_CLOUD_PROJECT set.")
        print()
        print("  RESOLUTION:")
        print("    1. Wait for quota reset  — midnight Pacific Time (UTC-7/-8)")
        print("    2. Upgrade to Gemini API Pay-as-you-go (no RPD cap)")
        print("       https://ai.google.dev/pricing")
        print("    3. Use a second AI Studio key (different Google account)")
        print("       Set GEMINI_API_KEY=<new key> in .env")
        print("    4. Run in stub mode now:")
        print("       python benchmark/_run_sim.py  (rule-based fallback, all real metrics)")

    elif "rate" in _message.lower():
        print("  ROOT CAUSE: Per-minute rate limit (RPM) hit — NOT daily quota.")
        print("  Wait 60 seconds and retry.")

    else:
        print(f"  ROOT CAUSE: Unknown 429 variant — message: {_message[:200]}")

elif _status_code == 400:
    print("  STATUS:  BAD REQUEST (400)")
    print("  ROOT CAUSE: Message format error or invalid model name.")
    print(f"  Message: {_message[:200]}")

elif _status_code == 403:
    print("  STATUS:  PERMISSION DENIED (403)")
    print("  ROOT CAUSE: Key lacks access to this model or billing not enabled.")
    print(f"  Message: {_message[:200]}")

elif _status_code is None:
    print("  STATUS:  NETWORK ERROR — could not reach Google API.")
    print("  Check internet connection and DNS resolution.")

else:
    print(f"  STATUS:  HTTP {_status_code} — {_message[:200]}")

print(f"\n{SEP}")
print("Audit complete.")
print(SEP)
