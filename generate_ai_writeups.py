"""
Generate plain-English "why it's strong / good" write-ups for each industry and
each leading stock, using TWO open models (Qwen + Llama) via an OpenAI-compatible
endpoint (OpenRouter by default). Each item is drafted independently by Qwen and
by Llama, then the two drafts are merged into one clean, beginner-friendly write-up.

Design goals
- Cheap & idempotent: results are cached in reports/ai_writeups_cache.json keyed by
  a hash of the *stable* facts (stage, quadrant, rounded fundamentals, thesis, ...),
  NOT daily price wiggles — so a daily CI run only pays for genuinely new/changed items.
- Safe in CI: if OPENROUTER_API_KEY is absent it writes whatever the cache already
  holds and exits 0 (never breaks the dashboard build).
- Provider-agnostic: set OPENROUTER_BASE_URL to hit Groq / Together / Ollama / etc.

Env
  OPENROUTER_API_KEY   (required to generate; without it, cache-only)
  OPENROUTER_BASE_URL  default https://openrouter.ai/api/v1
  QWEN_MODEL           default qwen/qwen-2.5-72b-instruct
  LLAMA_MODEL          default meta-llama/llama-3.3-70b-instruct
  MERGE_MODEL          default = QWEN_MODEL
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

HERE = Path(__file__).parent
DASHBOARD_DIR = HERE / "dashboard"
DATA_PATH = DASHBOARD_DIR / "dashboard_data.json"
OUT_PATH = DASHBOARD_DIR / "ai_writeups.json"
CACHE_PATH = HERE / "reports" / "ai_writeups_cache.json"

BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
API_KEY = os.getenv("OPENROUTER_API_KEY", "")
# Free OpenRouter models — the account has no credits, so the paid Qwen/Llama
# models return HTTP 402. Default to ONE free model per item (LLAMA blank => no
# second draft, no merge) to stretch the free daily request cap. If credits are
# added later, set QWEN_MODEL/LLAMA_MODEL/MERGE_MODEL env vars to restore the
# two-draft-then-merge flow with stronger models.
QWEN_MODEL = os.getenv("QWEN_MODEL", "google/gemma-4-31b-it:free")
LLAMA_MODEL = os.getenv("LLAMA_MODEL", "")
MERGE_MODEL = os.getenv("MERGE_MODEL", QWEN_MODEL)
SCOPE = os.getenv("AI_WRITEUP_SCOPE", "leading")
# Free OpenRouter models are limited to ~16 requests/MINUTE. Serialize (1 worker)
# and pace each request so we stay under that and finish all items in one run
# instead of getting 429-throttled. Bump AI_WRITEUP_WORKERS / lower
# AI_REQUEST_INTERVAL once credits raise the limit.
MAX_WORKERS = int(os.getenv("AI_WRITEUP_WORKERS", "1"))
REQUEST_INTERVAL = float(os.getenv("AI_REQUEST_INTERVAL", "4.0"))  # ~15/min < 16/min cap

# Bump to force every write-up to regenerate (e.g. after a prompt change).
PROMPT_VERSION = "v1"

SYSTEM = (
    "You are a plain-English equity educator writing for a complete beginner in India. "
    "Use short, simple sentences. Avoid jargon; if you must use a term, explain it in a few words. "
    "Be concrete and specific to the facts given. Focus on why it looks strong or good, but stay honest "
    "and mention one key risk briefly. Do NOT give buy/sell/hold advice or price targets. "
    "Never invent numbers that are not provided."
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


def _chat(model: str, system: str, user: str, timeout: int = 90) -> str:
    # Pace each call to stay under the free-tier per-minute cap; on a 429 wait
    # out the minute window and retry rather than failing the item.
    for _attempt in range(4):
        time.sleep(REQUEST_INTERVAL)
        resp = requests.post(
            f"{BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json",
                # OpenRouter-recommended attribution headers (harmless on other providers)
                "HTTP-Referer": "https://myfinancialria.github.io/weinstein-rrg-nse-dashboard/",
                "X-Title": "Weinstein RRG Dashboard",
            },
            json={
                "model": model,
                "temperature": 0.4,
                "max_tokens": 500,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            timeout=timeout,
        )
        if resp.status_code == 429:
            time.sleep(62)  # free per-minute window resets each minute
            continue
        break
    if resp.status_code >= 400:
        # surface the provider's reason (helps diagnose 400s in CI logs)
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"no choices in response: {json.dumps(data)[:300]}")
    return (choices[0].get("message", {}).get("content") or "").strip()


def _with_retry(fn, *args, tries: int = 3, **kwargs):
    last = None
    for attempt in range(tries):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - network/provider errors vary
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise last


def draft_prompt(kind: str, facts: dict) -> str:
    label = "sector/industry" if kind == "industry" else "company (stock)"
    ask = (
        "In very simple language a beginner can follow, explain why this "
        f"{label} looks strong or attractive right now. "
        "Write 4-6 short sentences OR 4-6 short bullet points. "
        "Cover the main points that apply: what it does / the demand for it, "
        "its trend and strength versus the market, its financial health, growth, "
        "and any government or policy support. End with one short honest risk. "
        "Use only the facts below.\n\nFACTS (JSON):\n"
    )
    return ask + json.dumps(facts, ensure_ascii=False, indent=2)


def merge_prompt(kind: str, name: str, draft_a: str, draft_b: str) -> str:
    what = "sector" if kind == "industry" else "stock"
    return (
        f"Two AI analysts wrote about the same {what} ({name}). Merge their notes into ONE "
        "clean write-up for a complete beginner. Keep every important and unique point, remove "
        "repetition, keep it short, simple and easy to read (a 3-5 sentence intro, then up to 5 "
        "short bullet points of the strongest reasons, then one short risk line). Plain English, "
        "no jargon, no buy/sell advice.\n\n"
        f"ANALYST 1 (Qwen):\n{draft_a}\n\nANALYST 2 (Llama):\n{draft_b}"
    )


def _safe_chat(model: str, system: str, user: str) -> str:
    """Best-effort call: returns '' instead of raising, so one model failing
    (e.g. a transient provider 400) never sinks the whole item."""
    try:
        return _with_retry(_chat, model, system, user)
    except Exception as exc:  # noqa: BLE001
        print(f"    (model {model} failed: {exc})")
        return ""


def generate_one(kind: str, name: str, facts: dict) -> str:
    prompt = draft_prompt(kind, facts)
    draft_q = _safe_chat(QWEN_MODEL, SYSTEM, prompt)
    # Second draft only when a second model is configured (blank = single-model,
    # 1 API call per item — best for the free daily cap).
    draft_l = _safe_chat(LLAMA_MODEL, SYSTEM, prompt) if LLAMA_MODEL else ""
    if not draft_q and not draft_l:
        raise RuntimeError("both Qwen and Llama drafts failed")
    if not draft_q:
        return draft_l
    if not draft_l:
        return draft_q
    # Merge; if the merge call fails, fall back to the fuller of the two drafts
    # so a flaky merge never costs us the item.
    merged = _safe_chat(MERGE_MODEL, SYSTEM, merge_prompt(kind, name, draft_q, draft_l))
    if merged:
        return merged
    return draft_q if len(draft_q) >= len(draft_l) else draft_l


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

    if todo and not API_KEY:
        print("OPENROUTER_API_KEY not set — writing cached results only, skipping generation.")
        todo = []

    if todo:
        def run(w):
            kind, name, key_id, facts, ck = w
            try:
                text = generate_one(kind, name, facts)
                return ck, text, None
            except Exception as exc:  # noqa: BLE001
                return ck, None, str(exc)

        done = 0
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futs = {ex.submit(run, w): w for w in todo}
            for fut in as_completed(futs):
                ck, text, err = fut.result()
                done += 1
                w = futs[fut]
                if text:
                    cache[ck] = text
                    if done % 10 == 0 or done == len(todo):
                        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
                        CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
                        print(f"  {done}/{len(todo)} generated (cache saved)")
                else:
                    print(f"  ! failed: {w[1]} — {err}")
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

    # Assemble output from cache
    out = {
        "generated_at": data.get("metrics", {}).get("generated_at", ""),
        "models": {"qwen": QWEN_MODEL, "llama": LLAMA_MODEL, "merge": MERGE_MODEL},
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
