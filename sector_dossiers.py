"""Researched narrative dossiers for NSE industry groups.

The daily sector-leadership report (build_sector_report.py) ranks industries by
RS-Ratio + RS-Momentum and renders these dossiers for whichever groups are in
the current top 10. The RANKING and RS figures refresh every run; this narrative
(schemes, Budget provisions, global forces) is web-researched and refreshed
periodically — it does not change day to day.

Each dossier keyed by the EXACT industry name in dashboard_data.json:
    scope         optional one-line definition of the group
    why           why it is leading (string; **bold** supported)
    opportunities future opportunities (string)
    schemes       government schemes & support (list of bullet strings)
    budget        latest Union Budget highlights (string)
    world         global challenges & opportunities (string)

Facts verified as of July 2026 (Union Budget FY2025-26 & FY2026-27). Refresh
periodically; update the `RESEARCHED_ON` date when you do.
"""

RESEARCHED_ON = "2026-07-03"

DOSSIERS = {
    "Non Banking Financial Company (NBFC)": {
        "why": (
            "Powered by the **RBI's rate-cutting cycle** — roughly **125 bps of repo cuts "
            "through 2025 to 5.25%**, held at the June 2026 MPC. Cheaper refinancing is "
            "expanding margins just as credit demand runs hot: NBFC credit grew **~20% in FY25 "
            "against banks' ~12%**, and NBFCs now originate **~41% of new personal-loan "
            "disbursements by value** (up from 27% two years earlier). Bajaj Finance returned "
            "+31.8% over the trailing year versus the Nifty's +6.6%."
        ),
        "opportunities": (
            "Analysts see sector AUM **crossing ₹50 lakh crore by March 2027**, with NBFC MSME "
            "AUM alone passing **₹5.3 lakh crore by FY26** (~32% CAGR over FY21–24). Growth "
            "vectors: co-lending, digital lending, gold loans and affordable housing finance."
        ),
        "schemes": [
            "**RBI Scale-Based Regulation (SBR)** — the four-layer prudential framework; an SBR review was initiated in December 2025.",
            "**RBI Co-Lending Arrangements Directions, 2025** — in force 1 January 2026, extending co-lending beyond priority-sector loans (10% minimum retention, DLG up to 5%).",
            "**Risk-weight rollback (February 2025)** — the RBI cut the risk weight on bank loans to well-rated NBFCs by 25 ppt (effective 1 April 2025).",
            "Revised **Priority-Sector-Lending Master Directions** (April 2025); SIDBI NBFC refinance of ₹64,189 crore at end-FY25.",
        ],
        "budget": (
            "**FY2025-26** raised the MSME credit-guarantee cover from ₹5 crore to ₹10 crore and "
            "launched a ₹5 lakh micro-enterprise credit card — both channel demand through NBFCs. "
            "**FY2026-27** positioned an “NBFC roadmap” under Viksit Bharat and proposed "
            "restructuring the PSU NBFCs PFC and REC."
        ),
        "world": (
            "A US Fed easing bias is a tailwind for large dollar-bond issuers, but **West Asia "
            "tensions in early 2026 pushed hedging costs up 50–75 bps**, prompting several NBFCs "
            "to pause external commercial borrowing. Smaller NBFCs still face domestic funding stress."
        ),
    },
    "Other Construction Materials": {
        "scope": (
            "Building materials excluding cement and steel — tiles & sanitaryware (Kajaria, Cera), "
            "plywood/laminates/MDF (Century, Greenlam), PVC/CPVC pipes (Supreme, Astral, Finolex), "
            "wires & cables (Polycab, KEI) and paints (Asian Paints, Berger)."
        ),
        "why": (
            "Two forces compound: a **housing and infrastructure capex upcycle**, and a powerful "
            "**formalisation shift** as GST and a wave of BIS Quality Control Orders move share to "
            "organised, listed players. Organised share in plywood is already ~62%; ceramic tiles "
            "are moving from ~46% (FY25) toward ~55% by FY29. The BIS Wood-Based Boards / Plywood "
            "QCO took effect 28 February 2025."
        ),
        "opportunities": (
            "Structural runways: tiles USD 10.45bn (2025) → ~16.7bn by 2031 (8.1% CAGR); wires & "
            "cables USD 21.2bn → 32.9bn by 2030 (9.1%); paints USD 11.5bn → 19.5bn by 2031 (9.3%). "
            "India is the world's largest tile exporter (Morbi); big-cap entrants — UltraTech "
            "(₹1,800 crore Bharuch cable plant, live December 2026), Adani, Aditya Birla — signal "
            "deep-pocketed confidence."
        ),
        "schemes": [
            "**PM Awas Yojana** — 3 crore new homes; PMAY-U 2.0 (1 crore urban) and PMAY-G extended to 2028-29 (₹3,06,137 crore outlay).",
            "**Jal Jeevan Mission (JJM 2.0)** — extended to December 2028, ₹8.69 trillion outlay, ~81.6% of rural households connected: a direct driver of pipe demand.",
            "**Smart Cities Mission** and a new **Urban Challenge Fund** (₹1 lakh crore corpus).",
        ],
        "budget": (
            "**FY2025-26** lifted PMAY (urban + rural) to ₹78,126 crore (+64% over RE) and JJM to "
            "₹67,000 crore. **FY2026-27** pushed total capital expenditure to **₹12.22 lakh crore** "
            "(₹17.15 lakh crore “effective”), with PMAY-U at ₹21,625 crore (+179%), PMAY-G "
            "at ₹54,917 crore (+69%) and JJM at ₹67,670 crore — a direct read-through to volumes."
        ),
        "world": (
            "India protects domestic makers with anti-dumping duties on Chinese inputs (PVC paste "
            "resin up to USD 707/t; CPVC resin USD 593–792/t to 2029), while facing external "
            "anti-dumping on tile exports. The key cost risk is energy — **Morbi's natural-gas spike "
            "in 2026 shut 400+ tile factories** before Gujarat cut gas prices in December 2025."
        ),
    },
    "Footwear": {
        "why": (
            "Footwear pairs the **highest RS-Ratio of the top ten** — the most established "
            "relative-strength trend — with a classic formalisation story. The organised segment is "
            "only ~30–35% of value while the unorganised sector is ~85% of production volume. The "
            "**September 2025 GST rationalisation** (5% on footwear ≤₹2,500/pair, 18% above) "
            "advantages compliant players; premium is now ~54% of value."
        ),
        "opportunities": (
            "Sports and athleisure footwear is the fast lane (→ USD 4.49bn by 2030); Campus "
            "Activewear extended into apparel in early 2026. Exports are projected to **cross USD "
            "6.5bn in FY26**, with **Tamil Nadu emerging as a non-leather hub** attracting Taiwanese "
            "contract makers under China+1."
        ),
        "schemes": [
            "**Indian Footwear & Leather Development Programme (IFLDP)** — ₹1,700 crore outlay (extended to 31 March 2026).",
            "**Focus Product Scheme for Footwear & Leather** — targets 22 lakh jobs, ₹4 lakh crore turnover and >₹1.1 lakh crore exports.",
            "**BIS QCO on footwear** (in force 1 August 2024) — mandatory licensing for 24 categories, a formalisation lever.",
        ],
        "budget": (
            "**FY2025-26** exempted crust and wet-blue leather from export/basic duties and launched "
            "the Focus Product Scheme. **FY2026-27** extended that support and widened it to "
            "non-leather footwear. No distinct new rupee allocation specific to footwear was confirmed "
            "in FY2026-27."
        ),
        "world": (
            "The dominant swing factor is **US tariffs**: the 2025 reciprocal tariff on India peaked "
            "at 50% (August 2025) before being cut to 18%. Leather & footwear exports dipped 0.23% "
            "to USD 3.3bn over April–December FY26. The opportunity is global brands (Clarks, "
            "Decathlon, Puma) sourcing from India; the risk is raw-material cost."
        ),
    },
    "Microfinance Institutions": {
        "why": (
            "A **turnaround leader**. MFIN's Q4 FY26 Micrometer shows **PAR (31–180 days) back to "
            "2.0% (March 2026) from 6.3% a year earlier** — pre-March-2024 levels — with 90+ DPD "
            "around ~1.4%. Gross Loan Portfolio reached **₹3.25 trillion in Q4 FY26, the first "
            "sequential growth after seven quarters of contraction**. The 5.25% repo and a "
            "rural-demand rebound are the tailwinds."
        ),
        "opportunities": (
            "GLP is seen reaching **~₹5–5.5 trillion by FY2027** (CareEdge), led by geographic "
            "deepening in eastern and central India, digitisation, and self-help-group linkage."
        ),
        "schemes": [
            "**RBI Regulatory Framework for Microfinance Loans, 2022** — collateral-free; household income ≤₹3 lakh; repayment capped at 50% of monthly household income.",
            "**MFIN self-regulatory guardrails** (effective 1 January 2025) — lenders per borrower cut from 4 to 3; total indebtedness capped at ₹2 lakh.",
            "Foundational rails: PMMY/MUDRA, PM Jan Dhan Yojana, NABARD SHG-Bank Linkage, SIDBI refinance, PSL classification.",
            "**Emerging risk — Bihar Microfinance Institutions Bill 2026**: Bihar is ~15% of the industry (~₹57,000 crore) and now faces state regulatory pressure.",
        ],
        "budget": (
            "The **MUDRA limit doubling to ₹20 lakh (“Tarun Plus”)** came in the July 2024 "
            "(FY2024-25) Budget, effective October 2024 — not February 2025. **FY2025-26** deepened "
            "SHG credit via “Lakhpati Didi”. **FY2026-27** raised the Gender Budget to "
            "₹1,07,688 crore and funded DAY-NRLM at ₹19,200 crore."
        ),
        "world": (
            "International impact investors (BlueOrchard, IIV Mikrofinanzfonds) remain active, and a "
            "softer global rate path helps sentiment. The principal risks are domestic: post-stress "
            "investor caution, state-level regulation (Bihar) and borrower over-indebtedness."
        ),
    },
    "Passenger Cars & Utility Vehicles": {
        "why": (
            "Riding **record volumes and a decisive mix shift to SUVs**. FY2025-26 domestic PV sales "
            "hit **~46.43 lakh units (+7.9% YoY)**, and April 2026 opened +25.4%. SUVs are now "
            "~53–57% of PV sales, lifting realisations and margins. Exports set a record at 9.05 lakh "
            "units (+17.5%), Maruti alone ~4.48 lakh (~49% share). Passenger-car EV penetration is "
            "~3.5–5% but growing 75–86% YoY."
        ),
        "opportunities": (
            "PV EV output is projected at **~1.33 million units — ~20% of PV production — by 2030**, "
            "alongside an export hub for Africa and the Gulf (top FY26 destinations South Africa, "
            "Saudi Arabia). Rising ADAS and electronics content per vehicle is a durable tailwind."
        ),
        "schemes": [
            "**PLI for Automobiles & Auto Components — ₹25,938 crore** (FY23–FY27; ₹1,350.83 crore disbursed by November 2025).",
            "**PM E-DRIVE — ₹10,900 crore** (Sept 2024–March 2026, the FAME-II successor; e-cars excluded).",
            "**SPMEPCI** — cuts CBU import duty to 15% for OEMs investing ≥₹4,150 crore (capped 8,000 units/yr).",
            "Vehicle Scrappage Policy and a rare-earth magnet incentive (₹7,280 crore, approved 2025).",
        ],
        "budget": (
            "**FY2025-26** fully exempted customs on cobalt powder, lithium-ion battery scrap and 12 "
            "critical minerals, and launched the **National Critical Mineral Mission (₹16,300 crore)**. "
            "**FY2026-27** raised the Auto PLI head to ₹5,939.87 crore and extended duty exemptions on "
            "critical-mineral processing capital goods."
        ),
        "world": (
            "The acute risk is **China's rare-earth magnet export curbs (April 2025)** — SIAM warned "
            "inventories were near-exhausted by end-May 2025, with production-halt risk. Exports "
            "(+24% in H1 FY26) offset. Other headwinds: Chinese-EV competition, US/EU tariffs, a "
            "global EV softening and semiconductor tightness."
        ),
    },
    "Meat Products including Poultry": {
        "why": (
            "A structural **protein-consumption upgrade** on rising incomes. India's poultry market "
            "is ~USD 32.9bn (2025). Per-capita intake is strikingly low — chicken ~6–7 kg/year versus "
            "20–25 globally, eggs ~102/year versus ~218 — so the runway is long. FY26 broiler volumes "
            "are up 6–8% and the market compounds ~8% toward ~USD 71.75bn by 2035."
        ),
        "opportunities": (
            "Two premium layers: **branded, ready-to-cook and online meat delivery** (~16% CAGR to "
            "~USD 119mn by 2030; Licious reportedly targeting a ~USD 2bn IPO), and **carabeef "
            "(buffalo-meat) exports** — total meat exports ~USD 5.1bn (FY24-25) to Vietnam, Malaysia, "
            "Egypt, the Gulf and Indonesia."
        ),
        "schemes": [
            "**Animal Husbandry Infrastructure Development Fund (AHIDF)** — revised outlay ₹29,610 crore, 3% interest subvention, up to 90% loan cover.",
            "**National Livestock Mission** — rural poultry entrepreneurship subsidy up to ₹25 lakh (50% of project cost).",
            "**National Animal Disease Control Programme** — ₹13,343 crore for 100% FMD and brucellosis vaccination.",
        ],
        "budget": (
            "The **Department of Animal Husbandry & Dairying allocation rose to ~₹6,153 crore in "
            "FY2026-27 (+~27%, a record)** from ₹4,840 crore, with Rashtriya Gokul Mission at ₹800 "
            "crore. Feed-grade maize was allowed at a concessional 15% duty via a tariff-rate quota "
            "to ease input costs."
        ),
        "world": (
            "The recurring hazard is **avian influenza (H5N1)** — 2025 saw ~602,000 birds culled in "
            "Andhra Pradesh, with fresh early-2026 outbreaks across Tamil Nadu, Andhra and Bihar. "
            "Feed is ~70% of variable cost, and CRISIL sees FY26 operating margins down ~50 bps on "
            "ethanol-driven maize demand. India's cost-advantaged carabeef holds a defensible niche."
        ),
    },
    "Telecom - Infrastructure": {
        "why": (
            "Riding the **5G build-out and a data explosion**. By late 2025 India had deployed "
            "**~518,000 5G base stations**, and 5G already carries ~35–47% of data traffic; average "
            "monthly data use per subscriber crossed ~31 GB. The footprint keeps expanding — ~8.43 "
            "lakh towers and optical fibre up to ~42.36 lakh route-km — yet **fiberisation is still "
            "only ~35–46% against a 75% target**, the biggest runway."
        ),
        "opportunities": (
            "The **Bharat 6G Alliance** targets ≥10% of global 6G patents by 2030; **data-centre "
            "capacity more than doubled in 2025 and is projected to triple to >4 GW by 2030** "
            "(~23% CAGR); plus small-cell densification, the fiberisation catch-up, rural BharatNet, "
            "IoT and overseas tower expansion."
        ),
        "schemes": [
            "**PLI for Telecom & Networking Products — ₹12,195 crore** (Feb 2021); by Sept 2025 it drove ~₹4,646 crore investment, ~₹96,240 crore net sales and ~60% import substitution across 42 firms.",
            "**Amended BharatNet Program (Phase III)** — Cabinet-approved Aug 2023, ₹1.39 lakh crore to connect ~6.4 lakh villages.",
            "**Digital Bharat Nidhi** — the erstwhile USOF, renamed under the Telecom Act 2023.",
        ],
        "budget": (
            "**FY2026-27** was decisively expansionary for digital infrastructure: **DoT outlay "
            "~₹73,990 crore (+39% over FY26 RE)**, **BharatNet raised sharply to ₹20,000 crore** "
            "(from ~₹5,500 crore RE), and a BSNL capital infusion of ~₹28,473 crore."
        ),
        "world": (
            "India's **“trusted-source” regime (December 2020)** effectively bars Huawei and "
            "ZTE from 5G, redirecting demand to approved vendors — an opportunity the PLI turned into "
            "genuine 4G/5G equipment export capability, with open-RAN and 6G IP next. The main risk is "
            "execution: BSNL/BharatNet Phase-III cost-overrun concerns surfaced in 2026."
        ),
    },
    "Auto Components & Equipments": {
        "why": (
            "Compounds the same SUV/premiumisation wave as the OEMs, with an added kicker — **content "
            "per vehicle is rising 20–25%**. ACMA reports FY25 industry turnover of **₹6.73 lakh crore "
            "(USD 80.2bn, +9.6% YoY)**, a near-doubling over five years. Exports rose ~8% to **USD "
            "22.9bn** (North America 32%, Europe 29.5%); the aftermarket reached ~₹99,948 crore."
        ),
        "opportunities": (
            "The **EV-component market is projected to grow from USD 7.8bn (2025) to USD 28.5bn "
            "(2030), ~29.6% CAGR**, and ACMA targets USD 100bn of exports by 2030. China+1 "
            "localisation, rising ADAS/semiconductor content, and precision-forging leaders (Bharat "
            "Forge, Sona BLW, Uno Minda) drive it."
        ),
        "schemes": [
            "**PLI Auto & Components (₹25,938 crore)** with a dedicated Component Champion track for advanced technologies.",
            "**PLI ACC Battery scheme (₹18,100 crore for 50 GWh)** — awardees include Reliance New Energy, Ola and Hyundai Global Motors.",
            "**PM E-DRIVE (₹10,900 crore)** and the **National Critical Mineral Mission (₹16,300 crore)**.",
        ],
        "budget": (
            "**FY2025-26** exempted basic customs on cobalt, lithium-ion battery scrap, lead, zinc "
            "and 12 critical minerals (25 minerals to zero duty). **FY2026-27** added capital-goods "
            "exemptions for lithium-cell manufacturing and announced rare-earth corridors (Odisha, "
            "Kerala, Andhra Pradesh, Tamil Nadu) and a ₹10,000 crore SME Growth Fund (the last two "
            "are single-trade-source — plausible, not officially confirmed)."
        ),
        "world": (
            "Two external forces dominate: **China's April-2025 rare-earth licensing** on seven "
            "elements could raise EV-motor costs 18–25%; and the **US Section 232 25% tariff on autos "
            "and parts (March 2025)** hits ~USD 6.5bn of Indian parts exports, with proposed relief to "
            "18% under a US-India deal."
        ),
    },
    "Medical Equipment & Supplies": {
        "why": (
            "Combines an **import-substitution** manufacturing story with structural healthcare "
            "demand. The market is ~**USD 15.2bn (2025), projected toward ~USD 50bn by 2030**; the "
            "domestic share of demand has risen from ~10% to ~30% over five years, though import "
            "dependence is still ~70–80%. Demand is amplified by insurance — **AB PM-JAY was extended "
            "in September 2024 to all citizens aged 70+** (₹5 lakh/family)."
        ),
        "opportunities": (
            "**Exports surged ~88% to ~₹31,120 crore (USD 3.64bn) in FY25** (consumables, implants, "
            "imaging, IVD kits), and PLI greenfield projects now produce **MRI, CT, mammography and "
            "ultrasound systems domestically** — the high-value localisation that lifts margins."
        ),
        "schemes": [
            "**National Medical Devices Policy 2023** — the umbrella framework targeting a global manufacturing hub.",
            "**PLI for Medical Devices — ₹3,420 crore** (FY23–FY27); 26 projects approved, ₹1,206 crore committed.",
            "**Medical Device Parks** — four central-scheme parks in Himachal Pradesh, Madhya Pradesh, Tamil Nadu and Uttar Pradesh.",
        ],
        "budget": (
            "**FY2025-26** fully exempted 36 lifesaving cancer/rare-disease drugs from basic customs "
            "duty and planned 200 cancer day-care centres. **FY2026-27** went further — full BCD "
            "exemption on 17 cancer drugs plus 7 rare-disease drugs, a new **BioPharma scheme of "
            "₹10,000 crore over five years**, a Health Ministry allocation of ~₹1,06,530 crore, and "
            "five PPP regional medical hubs."
        ),
        "world": (
            "The core vulnerability is upstream: **China is the single largest import source (~20%)** "
            "and dominates critical inputs (an estimated ~97% of silicon-wafer imports and MRI "
            "rare-earth magnets). On exports, US FDA and EU MDR/IVDR certification demand heavy "
            "clinical validation, and India's framework still lags EU-MDR harmonisation."
        ),
    },
    "Animal Feed": {
        "why": (
            "The mirror-image of the protein story — as poultry, dairy and aquaculture scale, so does "
            "scientifically formulated compound feed. The market is ~**₹1,186bn (2025), growing to "
            "₹2,113bn by 2034 (6.6% CAGR)**. Poultry feed is ~56% of the market; **aquafeed is the "
            "fast-growth pocket — USD 1.90bn (2025) → USD 4.26bn by 2035 (8.4% CAGR)** — on shrimp-"
            "export momentum (Avanti Feeds, Godrej Agrovet, Waterbase)."
        ),
        "opportunities": (
            "India posted **record seafood exports in FY2025-26 — 19.72 lakh tonnes worth ₹73,890 "
            "crore (USD 8.46bn)**, of which frozen shrimp was ₹49,038 crore (66.5% of dollar "
            "earnings, +8.6%). Value-added specialty feed and additives are the margin lever."
        ),
        "schemes": [
            "**PM Matsya Sampada Yojana (PMMSY)** and its sub-scheme **PM-MKSSY (~₹6,000 crore over FY24–FY27)** for formalising the fisheries value chain.",
            "**Fisheries & Aquaculture Infrastructure Development Fund (FIDF, ₹7,522 crore)**; National Livestock Mission feed component; KCC extended to fisheries.",
        ],
        "budget": (
            "**FY2025-26** cut basic customs duty on aqua-feed inputs — fish hydrolysate 15%→5%, "
            "frozen fish paste/surimi 30%→5%, shrimp broodstock 30%→5%. **FY2026-27** set a **record "
            "fisheries allocation of ₹2,761.80 crore with PMMSY at ₹2,500 crore**, plus support for "
            "34 fisheries clusters and 200 startups."
        ),
        "world": (
            "The pivotal variable is **US shrimp trade policy** (the US is India's #1 shrimp market by "
            "value). After the reciprocal tariff peaked near 50% in 2025, a **US-India deal on 2 "
            "February 2026 cut it to 18%** and removed the Russia-oil surtax; shrimp now carries ~18% "
            "plus antidumping (~3.76%) and countervailing (~5.77%) duties. A proposed "
            "“India Shrimp Tariff Act” (10%→20%→40% over 2026-28) is a legislative overhang, not law."
        ),
    },
}
