"""Generate the daily Sector Leadership report page.

Ranks NSE industry groups by combined RS-Ratio + RS-Momentum, takes the current
top 10, and renders a self-contained HTML report into dashboard/sector-leadership.html
(published with the rest of the dashboard on GitHub Pages).

The RANKING and RS figures come from the live dashboard_data.json, so they
refresh every run as the leadership rotates. The narrative per industry comes
from sector_dossiers.DOSSIERS (web-researched, refreshed periodically); any
industry that rotates into the top 10 without a dossier falls back to the
dashboard's INDUSTRY_THESES and finally to a data-driven writeup.

Run after build_dashboard_data.py:
    python build_dashboard_data.py && python build_sector_report.py
"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sector_dossiers import DOSSIERS, RESEARCHED_ON

try:
    from build_dashboard_data import INDUSTRY_THESES
except Exception:  # keep the report generatable even if the import chain breaks
    INDUSTRY_THESES = {}

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "dashboard" / "dashboard_data.json"
OUT_FILE = BASE_DIR / "dashboard" / "sector-leadership.html"

TOP_N = 10


# ----------------------------------------------------------------- text helpers
def md(text: str) -> str:
    """HTML-escape then render **bold**. Rupee/percent unicode passes through."""
    if not text:
        return ""
    out = html.escape(str(text))
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    return out


def esc(text: str) -> str:
    return html.escape(str(text or ""))


def num(x, dp=2):
    try:
        return f"{float(x):.{dp}f}"
    except (TypeError, ValueError):
        return "&mdash;"


def report_date() -> str:
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).strftime("%-d %B %Y")


# ----------------------------------------------------------------- dossier lookup
def dossier_for(industry: dict) -> dict:
    """Return a normalised dossier for an industry, with graceful fallback."""
    name = industry.get("name", "")
    if name in DOSSIERS:
        d = dict(DOSSIERS[name])
        d["_source"] = "researched"
        return d

    thesis = INDUSTRY_THESES.get(name)
    if thesis:
        schemes = []
        if thesis.get("policy_support"):
            schemes.append(thesis["policy_support"])
        return {
            "why": thesis.get("relevance_now", ""),
            "opportunities": thesis.get("future_possibilities", ""),
            "schemes": schemes,
            "budget": "",
            "world": "",
            "_source": "thesis",
        }

    # last-resort data-driven writeup
    ratio = industry.get("rs_ratio") or 0
    mom = industry.get("rs_momentum") or 0
    lead = "outperforming and accelerating" if ratio >= 100 else "led by improving momentum from a lagging base"
    return {
        "why": (
            f"This group screens into the leadership cohort on relative strength "
            f"(RS-Ratio {num(ratio)}, RS-Momentum {num(mom)}) — {lead} versus the broad market. "
            f"A researched dossier for this industry has not yet been added."
        ),
        "opportunities": "",
        "schemes": [],
        "budget": "",
        "world": "",
        "_source": "generic",
    }


# ----------------------------------------------------------------- rendering
DIMENSIONS = [
    ("why", "Why it&rsquo;s leading"),
    ("opportunities", "Future opportunities"),
    ("schemes", "Government schemes &amp; support"),
    ("budget", "Budget highlights"),
    ("world", "Global forces"),
]

CSS = """
  :root{
    --ink:#16211d; --paper:#f3f4f0; --raised:#fbfbf9;
    --teal:#0e5c4f; --teal-deep:#0a3f36; --amber:#a9670f;
    --rule:#dcded6; --rule-soft:#e7e8e1; --muted:#59635d; --up:#1f7a4d;
    --serif:"Iowan Old Style","Palatino Linotype",Palatino,"Book Antiqua",Georgia,serif;
    --sans:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    --measure:66ch;
  }
  *{box-sizing:border-box}
  html{scroll-behavior:smooth}
  body{background:var(--paper);color:var(--ink);font-family:var(--serif);
    -webkit-font-smoothing:antialiased;line-height:1.62;margin:0}
  .wrap{max-width:960px;margin:0 auto;padding:clamp(1.25rem,4vw,3.5rem) clamp(1.1rem,4vw,2.25rem) 5rem}
  .col{max-width:var(--measure)}
  .eyebrow{font-family:var(--sans);text-transform:uppercase;letter-spacing:.18em;
    font-size:.7rem;font-weight:600;color:var(--teal)}
  .num{font-variant-numeric:tabular-nums}
  a{color:var(--teal);text-underline-offset:2px}

  .back{font-family:var(--sans);font-size:.78rem;text-decoration:none;color:var(--muted);display:inline-block;margin-bottom:1rem}
  .back:hover{color:var(--teal)}

  .mast{border-top:3px solid var(--ink);padding-top:1.15rem;margin-bottom:2.6rem}
  .mast h1{font-size:clamp(2.05rem,5.2vw,3.35rem);line-height:1.04;font-weight:600;
    letter-spacing:-.012em;margin:.5rem 0 .35rem;text-wrap:balance;max-width:20ch}
  .mast .dek{font-size:clamp(1.05rem,2.4vw,1.32rem);color:var(--muted);font-style:italic;
    margin:0 0 1.15rem;max-width:52ch;text-wrap:balance}
  .meta{display:flex;flex-wrap:wrap;gap:.5rem 1.5rem;font-family:var(--sans);
    font-size:.78rem;color:var(--muted);border-top:1px solid var(--rule);padding-top:.8rem}
  .meta b{color:var(--ink);font-weight:600}

  h2.sec{font-size:.82rem;font-family:var(--sans);text-transform:uppercase;letter-spacing:.16em;
    color:var(--muted);font-weight:600;margin:0 0 1.1rem;padding-bottom:.5rem;border-bottom:1px solid var(--rule)}
  p{margin:0 0 1rem}
  .lead-para{font-size:1.14rem}

  .rank-wrap{overflow-x:auto;margin:1.6rem 0 0;border:1px solid var(--rule);border-radius:3px;background:var(--raised)}
  table{border-collapse:collapse;width:100%;font-family:var(--sans);font-size:.86rem}
  thead th{text-align:left;text-transform:uppercase;letter-spacing:.08em;font-size:.66rem;
    color:var(--muted);font-weight:600;padding:.7rem .85rem;border-bottom:1.5px solid var(--rule);white-space:nowrap}
  thead th.r{text-align:right}
  tbody td{padding:.62rem .85rem;border-bottom:1px solid var(--rule-soft);vertical-align:middle}
  tbody tr:last-child td{border-bottom:none}
  tbody tr:hover{background:#eef0ea}
  td.rk{font-variant-numeric:tabular-nums;color:var(--amber);font-weight:700;width:2.4rem}
  td.nm{font-family:var(--serif);font-size:1rem;font-weight:600;min-width:15rem}
  td.nm a{color:inherit;text-decoration:none}
  td.nm a:hover{color:var(--teal)}
  td.r{text-align:right;font-variant-numeric:tabular-nums;color:var(--ink)}
  .bar{min-width:120px}
  .bar .track{height:7px;background:#e4e6df;border-radius:4px;overflow:hidden}
  .bar .fill{height:100%;background:linear-gradient(90deg,var(--teal),#2f8a76);border-radius:4px}
  .bar .val{font-variant-numeric:tabular-nums;font-weight:600;font-size:.8rem;margin-bottom:.28rem;display:block}
  .note{font-family:var(--sans);font-size:.74rem;color:var(--muted);margin-top:.6rem}

  .leaders{margin-top:3.4rem;display:flex;flex-direction:column;gap:3.1rem}
  .leader{scroll-margin-top:1rem}
  .lhead{display:flex;align-items:baseline;gap:1rem;border-bottom:2px solid var(--ink);padding-bottom:.7rem}
  .lhead .rk{font-family:var(--sans);font-weight:700;font-size:clamp(1.9rem,5vw,2.7rem);
    color:var(--amber);line-height:.9;font-variant-numeric:tabular-nums;flex:none}
  .lhead h3{font-size:clamp(1.4rem,3.2vw,1.9rem);font-weight:600;letter-spacing:-.01em;margin:0;line-height:1.1;text-wrap:balance}
  .chips{display:flex;flex-wrap:wrap;gap:.4rem;margin:.85rem 0 1.4rem;font-family:var(--sans)}
  .chip{display:inline-flex;align-items:baseline;gap:.4rem;border:1px solid var(--rule);
    background:var(--raised);border-radius:100px;padding:.28rem .7rem;font-size:.72rem;color:var(--muted)}
  .chip b{font-variant-numeric:tabular-nums;color:var(--ink);font-weight:700;font-size:.82rem}
  .chip.hi{border-color:#bcd8cf;background:#eaf4f0}
  .chip.hi b{color:var(--teal-deep)}
  .scope{font-size:.95rem;color:var(--muted);border-left:2px solid var(--rule);padding-left:.9rem;margin-bottom:1.2rem}
  .thin{font-family:var(--sans);font-size:.72rem;color:var(--amber);margin-bottom:1rem}

  .dim{margin-top:1.25rem}
  .dim .lab{font-family:var(--sans);text-transform:uppercase;letter-spacing:.13em;font-size:.68rem;
    font-weight:600;color:var(--teal);display:flex;align-items:center;gap:.55rem;margin-bottom:.35rem}
  .dim .lab::before{content:"";width:14px;height:1.5px;background:var(--amber);flex:none}
  .dim p{max-width:var(--measure)}
  .dim ul{margin:.2rem 0 1rem;padding-left:0;list-style:none;max-width:var(--measure)}
  .dim li{position:relative;padding-left:1.15rem;margin-bottom:.5rem}
  .dim li::before{content:"";position:absolute;left:0;top:.62em;width:5px;height:5px;background:var(--teal);border-radius:50%}

  .synth{margin-top:3.6rem;border-top:3px solid var(--ink);padding-top:1.6rem}
  .synth h2{font-size:clamp(1.5rem,3.5vw,2rem);font-weight:600;margin:.2rem 0 1.1rem;letter-spacing:-.01em}
  .engines{display:grid;gap:1rem;grid-template-columns:1fr;margin:1.4rem 0}
  @media(min-width:720px){.engines{grid-template-columns:repeat(3,1fr)}}
  .engine{background:var(--raised);border:1px solid var(--rule);border-top:3px solid var(--teal);border-radius:3px;padding:1.05rem 1.1rem}
  .engine .k{font-family:var(--sans);font-size:.68rem;text-transform:uppercase;letter-spacing:.12em;color:var(--amber);font-weight:700;margin-bottom:.4rem}
  .engine h4{margin:0 0 .5rem;font-size:1.12rem;font-weight:600}
  .engine p{font-size:.92rem;margin:0;color:#39423d}
  .pull{background:var(--raised);border:1px solid var(--rule);border-left:3px solid var(--teal);
    border-radius:3px;padding:1.15rem 1.3rem;margin:1.6rem 0;max-width:var(--measure)}
  .pull p{margin:0;font-size:1.02rem}

  footer.disc{margin-top:3.4rem;border-top:1px solid var(--rule);padding-top:1.3rem;
    font-family:var(--sans);font-size:.78rem;line-height:1.6;color:var(--muted);max-width:78ch}
  footer.disc b{color:var(--ink)}

  @media (prefers-reduced-motion:no-preference){
    .leader,.mast,.rank-wrap,.synth{animation:rise .7s cubic-bezier(.2,.7,.2,1) both}
    @keyframes rise{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
  }
"""


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def render_dimension(key: str, label: str, dossier: dict) -> str:
    val = dossier.get(key)
    if not val:
        return ""
    if key == "schemes":
        if not isinstance(val, list) or not val:
            return ""
        items = "".join(f"<li>{md(b)}</li>" for b in val)
        body = f"<ul>{items}</ul>"
    else:
        body = f"<p>{md(val)}</p>"
    return f'<div class="dim"><div class="lab">{label}</div>{body}</div>'


def render_leader(rank: int, ind: dict) -> str:
    name = ind.get("name", "")
    ratio = ind.get("rs_ratio") or 0
    mom = ind.get("rs_momentum") or 0
    combined = ratio + mom
    dossier = dossier_for(ind)

    scope = ""
    if dossier.get("scope"):
        scope = f'<p class="scope"><b>Scope:</b> {md(dossier["scope"])}</p>'

    thin = ""
    if dossier.get("_source") == "generic":
        thin = '<p class="thin">Data-driven summary — researched dossier pending.</p>'
    elif dossier.get("_source") == "thesis":
        thin = '<p class="thin">Summary from industry thesis; scheme/budget detail pending.</p>'

    dims = "".join(render_dimension(k, lbl, dossier) for k, lbl in DIMENSIONS)

    return f"""
    <section class="leader" id="{slug(name)}">
      <div class="lhead"><span class="rk">{rank}</span><h3>{esc(name)}</h3></div>
      <div class="chips"><span class="chip">RS-Ratio <b>{num(ratio)}</b></span><span class="chip">RS-Momentum <b>{num(mom)}</b></span><span class="chip hi">Combined <b>{num(combined)}</b></span></div>
      {scope}{thin}{dims}
    </section>"""


def render(data: dict) -> str:
    industries = [i for i in data.get("industries", []) if i.get("name")]
    ranked = sorted(
        industries,
        key=lambda i: (i.get("rs_ratio") or 0) + (i.get("rs_momentum") or 0),
        reverse=True,
    )[:TOP_N]

    combos = [(i.get("rs_ratio") or 0) + (i.get("rs_momentum") or 0) for i in ranked]
    lo, hi = (min(combos), max(combos)) if combos else (0, 1)
    span = (hi - lo) or 1.0

    rows = []
    for n, ind in enumerate(ranked, 1):
        ratio = ind.get("rs_ratio") or 0
        mom = ind.get("rs_momentum") or 0
        combined = ratio + mom
        width = 22 + (combined - lo) / span * 73
        rows.append(
            f'<tr><td class="rk">{n}</td>'
            f'<td class="nm"><a href="#{slug(ind["name"])}">{esc(ind["name"])}</a></td>'
            f'<td class="r num">{num(ratio)}</td><td class="r num">{num(mom)}</td>'
            f'<td class="bar"><span class="val">{num(combined)}</span>'
            f'<div class="track"><div class="fill" style="width:{width:.0f}%"></div></div></td></tr>'
        )

    leaders = "".join(render_leader(n, ind) for n, ind in enumerate(ranked, 1))
    date = report_date()

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>India Sector Leadership Report &mdash; {date}</title>
<meta name="description" content="India's strongest NSE industries ranked by combined RS-Ratio and RS-Momentum, with drivers, government schemes, Budget provisions and global forces.">
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <a class="back" href="index.html">&larr; Back to dashboard</a>
  <header class="mast">
    <div class="eyebrow">NSE Industry Rotation &middot; Relative Strength Leaders</div>
    <h1>India&rsquo;s Strongest Industries</h1>
    <p class="dek">A momentum-ranked research analysis of the industry groups leading the market on combined RS-Ratio and RS-Momentum &mdash; and the fundamentals, policy and global forces behind each.</p>
    <div class="meta">
      <span><b>As of</b> {date}</span>
      <span><b>Universe</b> {len(industries)} NSE industry groups vs. benchmark</span>
      <span><b>Ranking</b> RS-Ratio + RS-Momentum (RRG)</span>
      <span><b>Narrative</b> web-researched, last refreshed {esc(RESEARCHED_ON)}</span>
    </div>
  </header>

  <section>
    <h2 class="sec">How the leaders were chosen</h2>
    <div class="col">
      <p class="lead-para">These are the {TOP_N} NSE industry groups ranking highest on a <b>combined Relative Strength score</b> &mdash; the sum of <b>RS-Ratio</b> (the trend of an industry&rsquo;s performance relative to the broad market) and <b>RS-Momentum</b> (the rate of change of that relative trend). A high combined score marks groups that are both <em>outperforming</em> the market and <em>accelerating</em>: the signature of sector leadership. <b>The ranking refreshes daily as leadership rotates.</b></p>
    </div>
    <div class="rank-wrap">
      <table>
        <thead><tr><th class="r">#</th><th>Industry</th><th class="r">RS-Ratio</th><th class="r">RS-Mom.</th><th>Combined strength</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </div>
    <p class="note">Bars scale the combined score within the current top-ten band to make the spread legible; a reading above 100 on either axis denotes market outperformance.</p>
  </section>

  <div class="leaders">{leaders}</div>

  <section class="synth">
    <div class="eyebrow">Reading across the leaders</div>
    <h2>Three engines drive the leadership board</h2>
    <div class="engines">
      <div class="engine"><div class="k">Engine 01</div><h4>The credit cycle</h4><p>The RBI&rsquo;s ~125 bps of 2025 easing to 5.25% lifts margins, and a supportive regulatory turn does the rest &mdash; with microfinance adding a turnaround as asset quality normalises.</p></div>
      <div class="engine"><div class="k">Engine 02</div><h4>Consumption &amp; formalisation</h4><p>GST rationalisation and a wave of BIS Quality Control Orders transfer share from the unorganised sector to listed players, atop premiumisation and a housing/infra upcycle.</p></div>
      <div class="engine"><div class="k">Engine 03</div><h4>Capex &amp; China+1</h4><p>The PLI architecture, the National Critical Mineral Mission and rising technology content per unit anchor the manufacturing-investment leg of leadership.</p></div>
    </div>
    <div class="pull"><p><b>The common policy tailwind.</b> Almost every leader touches record public capital expenditure (&#8377;12.22 lakh crore in FY2026-27), the PLI incentive stack, and formalisation via GST and BIS Quality Control Orders.</p></div>
    <div class="pull" style="border-left-color:var(--amber)"><p><b>The common global risk.</b> Two threads recur: <b>US tariff policy</b> (a 2025 spike toward ~50%, cut to ~18% via the February-2026 US-India deal) and <b>critical-mineral dependence on China</b>. Both are being mitigated but neither is fully resolved.</p></div>
  </section>

  <footer class="disc">
    <p><b>Methodology.</b> Rankings are derived from the NSE industry-group Relative Rotation Graph, combining RS-Ratio and RS-Momentum versus the benchmark; they reflect price-relative strength as of {date} and rotate over time. The per-industry narrative was web-verified from government (PIB, indiabudget.gov.in, RBI, DAHD, BIS), rating-agency (CareEdge, CRISIL) and industry-body (SIAM, ACMA, MFIN, MPEDA) sources and was last refreshed on {esc(RESEARCHED_ON)}; market-size and multi-year projections are third-party estimates and directional.</p>
    <p style="margin-top:.7rem"><b>Disclaimer.</b> This is a research and educational analysis, not investment advice. Relative-strength leadership is a momentum signal, not a valuation or quality judgement.</p>
  </footer>
</div>
</body>
</html>
"""


def main():
    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    OUT_FILE.write_text(render(data), encoding="utf-8")
    ranked = sorted(
        (i for i in data.get("industries", []) if i.get("name")),
        key=lambda i: (i.get("rs_ratio") or 0) + (i.get("rs_momentum") or 0),
        reverse=True,
    )[:TOP_N]
    covered = sum(1 for i in ranked if i.get("name") in DOSSIERS)
    print(f"sector report: wrote {OUT_FILE.name} — top {len(ranked)} industries, "
          f"{covered} with researched dossiers.")
    for n, i in enumerate(ranked, 1):
        tag = "" if i["name"] in DOSSIERS else ("  [thesis]" if i["name"] in INDUSTRY_THESES else "  [generic]")
        print(f"  {n:2}. {i['name']}{tag}")


if __name__ == "__main__":
    main()
