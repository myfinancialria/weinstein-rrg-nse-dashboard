"""
Generate plain-English "why it's strong / good" write-ups for each industry and
each leading stock, using TWO open models (Qwen + Llama) via an OpenAI-compatible
endpoint (OpenRouter by default). Each item is drafted independently by Qwen and
by Llama, then the two drafts are merged into one clean, beginner-friendly write-up.

Design goals
- Cheap & idempotent: results are cached in reports/ai_writeups_cache.json keyed by
  a hash of the *stable* facts (stage, quadrant, rounded fundamentals, thesis, ...),
  NOT daily price wiggles — so a daily CI run only pays for genuinely new/changed items.
- Safe in CI: if no provider key is set it writes whatever the cache already
  holds and exits 0 (never breaks the dashboard build).
- Multi-provider with per-day caps + failover: tries Gemini, then Groq, then
  OpenRouter (order/keys configurable). Each provider has a per-day request cap
  kept UNDER its free tier and a per-minute pacing interval; when a provider hits
  its daily cap or quota we fall through to the next. Per-day counts persist in
  reports/ai_usage.json (reset each UTC day), so the caps hold across all runs in
  a day. A wall-clock budget stops the step so it can't hang the deploy; the
  cache fills over successive runs.

Env (set only the provider keys you have)
  GEMINI_API_KEY / GROQ_API_KEY / OPENROUTER_API_KEY   provider keys
  AI_PROVIDER_ORDER    default "gemini,groq,openrouter"
  GEMINI_MODEL (gemini-2.5-flash) / GROQ_MODEL (llama-3.3-70b-versatile) / OPENROUTER_MODEL
  GEMINI_DAILY_CAP (1400) / GROQ_DAILY_CAP (900) / OPENROUTER_DAILY_CAP (40)
  GEMINI_INTERVAL / GROQ_INTERVAL / OPENROUTER_INTERVAL   per-request pacing (s)
  AI_WRITEUP_BUDGET_SECS (600)   wall-clock cap for the whole step
  AI_WRITEUP_SCOPE     "leading" (default: 44 industries + leading stocks) | "all"

Output
  dashboard/ai_writeups.json  ->  {generated_at, models, version,
                                   industries:{name:text}, stocks:{symbol:text}}
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).parent
DASHBOARD_DIR = HERE / "dashboard"
DATA_PATH = DASHBOARD_DIR / "dashboard_data.json"
OUT_PATH = DASHBOARD_DIR / "ai_writeups.json"
CACHE_PATH = HERE / "reports" / "ai_writeups_cache.json"

def _env(name: str, default: str) -> str:
    """os.getenv but treat an empty string (e.g. an unset GitHub `vars.X`) as
    unset, so empty CI variables fall back to the default instead of blanking it."""
    v = os.getenv(name)
    return v if v not in (None, "") else default


SCOPE = _env("AI_WRITEUP_SCOPE", "leading")
BUDGET_SECS = float(_env("AI_WRITEUP_BUDGET_SECS", "600"))  # cap the step so it can't hang the run
USAGE_PATH = HERE / "reports" / "ai_usage.json"  # per-day request counts (committed, persists across runs)

# --- Multi-provider chain (all OpenAI-compatible /chat/completions) ---
# Each provider is tried in order; when one hits its per-day cap or its quota is
# exhausted we fall through to the next. Set only the keys you have — providers
# without a key are skipped. Per-day caps stay UNDER each free tier so we never
# blow the daily allowance; `interval` paces under the per-minute limit.
_PROVIDER_DEFS = {
    "gemini": {
        "base_url": _env("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai"),
        "key": os.getenv("GEMINI_API_KEY", ""),
        "model": _env("GEMINI_MODEL", "gemini-2.5-flash"),
        "daily_cap": int(_env("GEMINI_DAILY_CAP", "1400")),  # free tier ~1500 RPD
        "interval": float(_env("GEMINI_INTERVAL", "4.5")),   # ~13/min < 15 RPM
    },
    "groq": {
        "base_url": _env("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
        "key": os.getenv("GROQ_API_KEY", ""),
        "model": _env("GROQ_MODEL", "llama-3.3-70b-versatile"),
        "daily_cap": int(_env("GROQ_DAILY_CAP", "900")),     # free tier ~1000 RPD
        "interval": float(_env("GROQ_INTERVAL", "2.2")),     # ~27/min < 30 RPM
    },
    "mistral": {
        "base_url": _env("MISTRAL_BASE_URL", "https://api.mistral.ai/v1"),
        "key": os.getenv("MISTRAL_API_KEY", ""),
        "model": _env("MISTRAL_MODEL", "mistral-small-latest"),  # free-tier quality model
        "daily_cap": int(_env("MISTRAL_DAILY_CAP", "800")),  # free "Experiment" tier is generous
        "interval": float(_env("MISTRAL_INTERVAL", "1.2")),  # free tier ~1 req/s
    },
    "openrouter": {
        "base_url": _env("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        "key": os.getenv("OPENROUTER_API_KEY", ""),
        "model": _env("OPENROUTER_MODEL", "google/gemma-4-31b-it:free"),
        "daily_cap": int(_env("OPENROUTER_DAILY_CAP", "40")),  # $0-account free tier is tiny
        "interval": float(_env("OPENROUTER_INTERVAL", "4.0")),
    },
    "deepseek": {
        "base_url": _env("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        "key": os.getenv("DEEPSEEK_API_KEY", ""),
        "model": _env("DEEPSEEK_MODEL", "deepseek-chat"),
        # DeepSeek has NO perpetual free tier: pay-as-you-go (cheap). This cap only
        # spends trial/prepaid credits. Keep it LOW so it can't run up a bill.
        "daily_cap": int(_env("DEEPSEEK_DAILY_CAP", "50")),
        "interval": float(_env("DEEPSEEK_INTERVAL", "1.5")),
    },
    "grok": {
        "base_url": _env("GROK_BASE_URL", "https://api.x.ai/v1"),
        "key": os.getenv("GROK_API_KEY", ""),
        "model": _env("GROK_MODEL", "grok-3-mini"),  # cheapest xAI model
        # xAI has NO perpetual free tier: this cap only spends the promotional
        # data-sharing credits. Keep it LOW and LAST so it can't run up a bill.
        "daily_cap": int(_env("GROK_DAILY_CAP", "50")),
        "interval": float(_env("GROK_INTERVAL", "1.5")),
    },
}
# Order matters: truly-free tiers first (Gemini, Groq, Mistral), then the tiny/
# credit-limited spillover providers (OpenRouter, DeepSeek, Grok). Each is used
# only after the ones before it hit their per-day cap.
PROVIDER_ORDER = [n.strip() for n in _env("AI_PROVIDER_ORDER", "gemini,groq,mistral,openrouter,deepseek,grok").split(",") if n.strip()]
PROVIDERS = [dict(_PROVIDER_DEFS[n], name=n) for n in PROVIDER_ORDER
             if n in _PROVIDER_DEFS and _PROVIDER_DEFS[n]["key"]]

# Bump to force every write-up to regenerate (e.g. after a prompt change).
PROMPT_VERSION = "v2"

SYSTEM = (
    "You are a seasoned Indian equity research writer explaining a company or sector to a "
    "smart beginner. Write in clear, professional plain English — the tone of a good analyst "
    "note, not marketing copy and not a textbook. "
    "Rules: "
    "(1) Short, direct sentences. One idea per sentence. "
    "(2) No jargon unless you immediately explain it in a few plain words. "
    "(3) Be specific and grounded — use the actual facts and numbers given, and say what each "
    "number means (e.g. 'ROCE of 22% means it earns good returns on the money it uses'). "
    "(4) Stay honest and balanced: explain why it looks strong, then give one real risk. "
    "(5) Never exaggerate, never hype, never use words like 'guaranteed', 'must-buy', or 'sure'. "
    "(6) No buy/sell/hold advice, no price targets, no invented numbers. "
    "Sound trustworthy and calm, like you are helping a friend understand, not selling to them."
)


def _num(v, nd=1):
    try:
        if v is None:
            return None
        f = float(v)
        if f != f:  # NaN
            return None
        return round(f, nd)
    except (TypeError, ValueError):
        return None


def industry_facts(ind: dict, thesis: dict) -> dict:
    return {
        "industry": ind.get("name"),
        "broad_sector": ind.get("parent") or ind.get("nse_industry"),
        "weinstein_stage": ind.get("stage"),
        "rrg_quadrant": ind.get("rrg_quadrant"),
        "outperforming_market": (_num(ind.get("rs_ratio")) or 0) > 100,
        "momentum_accelerating": (_num(ind.get("rs_momentum")) or 0) > 100,
        "companies_in_group": ind.get("member_count"),
        "why_relevant_now": thesis.get("relevance_now"),
        "future_possibilities": thesis.get("future_possibilities"),
        "policy_support": thesis.get("policy_support"),
    }


def stock_facts(s: dict, thesis: dict) -> dict:
    return {
        "name": s.get("name"),
        "symbol": s.get("display_symbol") or s.get("symbol"),
        "industry": s.get("parent"),
        "product_or_business": s.get("common_product_business") or s.get("yahoo_industry"),
        "company_description": (s.get("company_description") or "")[:900],
        "in_healthy_uptrend_stage2": s.get("stage") == "stage_2",
        "rrg_quadrant": s.get("rrg_quadrant"),
        "pe": _num(s.get("pe_ratio") or s.get("pe")),
        "roce_pct": _num(s.get("roce")),
        "roe_pct": _num(s.get("roe_pct")),
        "net_margin_pct": _num(s.get("net_margin_pct")),
        "operating_margin_pct": _num(s.get("operating_margin_pct")),
        "debt_to_equity": _num(s.get("debt_to_equity")),
        "qtr_sales_growth_pct": _num(s.get("qtr_sales_var_pct")),
        "qtr_profit_growth_pct": _num(s.get("qtr_profit_var_pct")),
        "market_cap_cr": _num(s.get("market_cap_cr")),
        "strengths": (s.get("swot") or {}).get("strengths"),
        "positive_points": s.get("pros"),
        "industry_tailwind": thesis.get("relevance_now"),
    }


def cache_key(kind: str, facts: dict) -> str:
    blob = json.dumps({"k": kind, "v": PROMPT_VERSION, "f": facts}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


class AllProvidersExhausted(Exception):
    """Every configured provider has hit its per-day cap (or has no key)."""


def _load_usage() -> dict:
    try:
        u = json.loads(USAGE_PATH.read_text(encoding="utf-8"))
    except Exception:
        u = {}
    today = time.strftime("%Y-%m-%d", time.gmtime())  # UTC day key
    if u.get("date") != today:
        u = {"date": today, "used": {}}
    u.setdefault("used", {})
    return u


def _save_usage(u: dict) -> None:
    USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    USAGE_PATH.write_text(json.dumps(u, indent=2), encoding="utf-8")


def _is_daily_quota(resp) -> bool:
    """A 429 that means the DAY's allowance is gone (vs a per-minute burst)."""
    if resp.status_code != 429:
        return False
    t = (resp.text or "").lower()
    return any(k in t for k in ("per day", "/day", "per-day", "daily", "requests per day",
                                "quota", "resource_exhausted", "rpd", "free-models-per-day"))


def _post(provider: dict, system: str, user: str, timeout: int = 90):
    return requests.post(
        f"{provider['base_url'].rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {provider['key']}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://myfinancialria.github.io/weinstein-rrg-nse-dashboard/",
            "X-Title": "Weinstein RRG Dashboard",
        },
        json={
            "model": provider["model"],
            "temperature": 0.4,
            "max_tokens": 500,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        },
        timeout=timeout,
    )


def generate_one(kind: str, name: str, facts: dict, usage: dict):
    """One write-up via the provider chain, honoring each provider's per-day cap.
    Falls through to the next provider on a per-minute 429 or a provider error,
    and marks a provider done-for-today on a daily-quota 429. Returns
    (text, provider_name) or raises AllProvidersExhausted when every provider is
    capped/keyless."""
    prompt = draft_prompt(kind, facts)
    used = usage["used"]
    any_available = False
    for p in PROVIDERS:
        if used.get(p["name"], 0) >= p["daily_cap"]:
            continue  # this provider is done for today
        any_available = True
        time.sleep(p["interval"])  # pace under the per-minute limit
        try:
            resp = _post(p, SYSTEM, prompt)
        except Exception as exc:  # network/timeout — try the next provider
            print(f"    ({p['name']} error: {exc}); trying next provider")
            continue
        if resp.status_code == 200:
            used[p["name"]] = used.get(p["name"], 0) + 1  # count a successful request
            _save_usage(usage)
            data = resp.json()
            choices = data.get("choices") or []
            text = (choices[0].get("message", {}).get("content") or "").strip() if choices else ""
            if text:
                return text, p["name"]
            print(f"    ({p['name']} returned empty); trying next provider")
            continue
        if resp.status_code == 429:
            if _is_daily_quota(resp):
                used[p["name"]] = p["daily_cap"]  # exhausted for the rest of the day
                _save_usage(usage)
                print(f"    ({p['name']} daily quota reached — switching to next provider)")
            else:
                print(f"    ({p['name']} per-minute 429 — switching to next provider)")
            continue
        if resp.status_code in (402, 403):
            # Out of credits / billing gate (e.g. xAI Grok after promo credits, or
            # OpenRouter with $0). Mark done-for-today so we stop hammering it.
            used[p["name"]] = p["daily_cap"]
            _save_usage(usage)
            print(f"    ({p['name']} HTTP {resp.status_code} (out of credits/billing) — done for today, switching)")
            continue
        print(f"    ({p['name']} HTTP {resp.status_code}: {resp.text[:150]}); trying next provider")
    if not any_available:
        raise AllProvidersExhausted()
    raise RuntimeError("all providers failed for this item")


def draft_prompt(kind: str, facts: dict) -> str:
    label = "sector/industry" if kind == "industry" else "company (stock)"
    ask = (
        "In simple, professional language a beginner can follow, explain why this "
        f"{label} looks strong or attractive right now. "
        "Write 4-6 short sentences OR 4-6 short bullet points. "
        "Start with the single most important reason it stands out. Then cover the "
        "points that apply: what it does and the demand for it, its price trend and "
        "strength versus the overall market, its financial health and growth (explain "
        "what the key numbers mean), and any government or policy tailwind. "
        "Finish with one short, honest risk in its own sentence. "
        "Keep it factual and calm — no hype, no advice, no price targets. "
        "Use only the facts below.\n\nFACTS (JSON):\n"
    )
    return ask + json.dumps(facts, ensure_ascii=False, indent=2)


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return default
    return default


def main() -> int:
    data = load_json(DATA_PATH, None)
    if not data:
        sys.exit(f"Cannot read {DATA_PATH}; run build_dashboard_data.py first.")

    theses = data.get("industryTheses", {})
    industries = [i for i in data.get("industries", []) if isinstance(i, dict) and i.get("name")]
    if SCOPE == "all":
        stocks = data.get("stocks", [])
    else:
        stocks = data.get("leadingStocks", []) or data.get("fundamentalPicks", [])
    # de-dupe stocks by symbol, keep those we can identify
    seen, stock_list = set(), []
    for s in stocks:
        sym = s.get("display_symbol") or s.get("symbol")
        if sym and sym not in seen:
            seen.add(sym)
            stock_list.append(s)

    # Build the work list of (kind, name, key_id, facts, cache_key)
    work = []
    for ind in industries:
        f = industry_facts(ind, theses.get(ind.get("name"), {}))
        work.append(("industry", ind["name"], ind["name"], f, cache_key("industry", f)))
    for s in stock_list:
        sym = s.get("display_symbol") or s.get("symbol")
        f = stock_facts(s, theses.get(s.get("parent"), {}))
        work.append(("stock", s.get("name") or sym, sym, f, cache_key("stock", f)))

    cache = load_json(CACHE_PATH, {})
    todo = [w for w in work if w[4] not in cache]
    print(f"AI write-ups: {len(work)} items ({len(industries)} industries + {len(stock_list)} stocks); "
          f"{len(work) - len(todo)} cached, {len(todo)} to generate.")

    usage = _load_usage()
    if todo and not PROVIDERS:
        print("No AI provider key set (GEMINI_API_KEY / GROQ_API_KEY / OPENROUTER_API_KEY) — "
              "writing cached results only, skipping generation.")
        todo = []
    elif todo:
        caps = ", ".join(f"{p['name']}={p['daily_cap']}(used {usage['used'].get(p['name'], 0)})" for p in PROVIDERS)
        print(f"providers (in order): {caps}")

    if todo:
        # Serial + wall-clock budget so the step NEVER hangs the pipeline. Each
        # item goes to the first provider with per-day quota left; when a provider
        # is exhausted we fall through to the next. Cache + usage persist across
        # runs, so remaining items resume next run/day. AllProvidersExhausted =>
        # every provider capped for the day; stop and let tomorrow finish.
        def _save():
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

        deadline = time.monotonic() + BUDGET_SECS
        done = ok = 0
        for w in todo:
            if time.monotonic() > deadline:
                print(f"  time budget ({BUDGET_SECS:.0f}s) reached — {ok} generated this run, "
                      f"{len(todo) - done} left for the next run.")
                break
            kind, name, key_id, facts, ck = w
            done += 1
            try:
                text, prov = generate_one(kind, name, facts, usage)
                if text:
                    cache[ck] = text
                    ok += 1
                    if ok % 5 == 0:
                        _save()
                        print(f"  {ok} generated (cache saved)")
            except AllProvidersExhausted:
                print(f"  all providers hit their daily cap — {ok} generated this run; "
                      f"remaining {len(todo) - done + 1} resume tomorrow.")
                break
            except Exception as exc:  # noqa: BLE001
                print(f"  ! failed: {name} — {exc}")
        _save()
        _save_usage(usage)
        print(f"  usage today (UTC): {usage['used']}")
        print(f"  AI write-ups this run: {ok} generated, {len(work) - len([w for w in work if w[4] in cache])} still to do.")

    # Assemble output from cache
    out = {
        "generated_at": data.get("metrics", {}).get("generated_at", ""),
        "models": {p["name"]: p["model"] for p in PROVIDERS} or {"none": ""},
        "version": PROMPT_VERSION,
        "industries": {},
        "stocks": {},
    }
    for kind, name, key_id, facts, ck in work:
        text = cache.get(ck)
        if not text:
            continue
        if kind == "industry":
            out["industries"][key_id] = text
        else:
            out["stocks"][key_id] = text

    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {OUT_PATH}: {len(out['industries'])} industries, {len(out['stocks'])} stocks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
