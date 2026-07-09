# Scaleway setup — VERIFIED pricing + recommendation (2026-07-08, from scaleway.com)

> FACTS from live pages. All instance prices EXCLUDE block storage + public IPv4 (add those).
> This is a gate-#12 PROVISIONING DECISION (record now, provision when the user clears #12). Region: nl-ams (EU).

## Compute instances (EUR/mo, storage+IPv4 excluded) — /en/pricing/virtual-instances/
| Instance | vCPU | RAM | €/mo | note |
|---|---|---|---|---|
| STARDUST1-S | 1 | 1 GB | €0.43 | too small |
| DEV1-S | 2 | 2 GB | €6.55 | too tight for 1.6GB corpus + services |
| **DEV1-M** | 3 | 4 GB | **€14.74** | budget pilot (shared cores) |
| **DEV1-L** | 4 | 8 GB | **€31.27** | headroom (shared cores) — RECOMMENDED for corpus + multi-service |
| BASIC2-A2C-4G | 2 | 4 GB | €16.79 | GP, more consistent perf |
| BASIC2-A4C-8G | 4 | 8 GB | €37.74 | GP 4c/8G |
| PLAY2-MICRO | 4 | 8 GB | €40.20 | cost-optimized |
+ block storage ~€0.0993/GB/mo (20 GB ≈ €2/mo) + public IPv4 (~€3/mo).

## Managed PostgreSQL (EUR/mo main node) — /en/pricing/managed-databases/
| Node | vCPU | RAM | €/mo | 
|---|---|---|---|
| **DB-DEV-S** | 2 | 2 GB | **€11.23** | cheapest managed |
| DB-DEV-M | 3 | 4 GB | €27.5 |
| DB-DEV-L | 4 | 8 GB | €54.5 |
+ storage (Block 5K €0.0993/GB/mo) + backups €0.03/GB/mo.
**Serverless SQL Database** = €0.13572/vCPU/hr + €0.000272/GB-hr storage → scales to zero, pay-per-use (good for low traffic).

## Other verified facts
- **Generative APIs – Serverless (hosted in EUROPE)** — Scaleway-native LLM inference, EU-resident. (We use Gemma-via-Google-AIS for $0; this is the EU-residency alternative if anchor content must stay in EU.)
- **Serverless Containers/Functions/Jobs** — scale-to-zero compute (could host the bursty Hramatka bake like HF's scale-to-zero, if we don't want it always-on).
- **Secret Manager** + **Key Manager** exist (AIS key etc.).
- **Startup credits**: Startup Program up to **€36,000** · Early Stage up to **€9,000** (6mo) · Founders up to **€1,000**. **Public Sector** solution (governments/research/HIGHER EDUCATION). → potentially €0 for the pilot IF eligible. VERIFY eligibility (non-commercial ed project) — do NOT assume.

## ⚖️ ELIGIBILITY — VERIFIED 2026-07-08 (live Scaleway pages; user order #2). VERDICT: no clean fit.
Checked all four vehicles against "non-commercial, INDIVIDUAL-run, non-institutional Ukrainian ed project":
1. **Startup Program (Founders/Early/Growth)** — stated criteria = *"< 5 years old · < 50 employees · not yet a Scaleway client"* (startup-program page FAQ). BUT: framed for *"startups going to market"*; **Early Stage requires "starting €500/mo consumption", Growth "starting €1500/mo consumption"** (min-spend thresholds a ~€20–36/mo pilot won't hit); third-party (startup-perks.com) states **nonprofits & students not eligible by default**. → €9k/€36k tiers effectively OUT (consumption floor). Only the **Founders €1,000 one-time voucher** (12mo, "migrate/build", no stated min-consumption) is a plausible apply-anyway — still nominally "startup", acceptance doubtful for a non-commercial individual.
2. **Scaleway Learning** — paid CERTIFICATION exams (€300+VAT), **NOT** a credits program. Irrelevant.
3. **Public Sector Solutions** — a **procurement/sales channel** via UGAP (French state procurement) for governments/public+research institutions/higher-ed *establishments*. **No credits**, not for individuals.
4. **Academia Program** — exists as a partnership initiative (GENCI/CNRS, French research institutions); page body unretrievable — institution-oriented, not an individual/non-French/non-commercial track.
**BOTTOM LINE:** the "potentially €0" is **NOT confirmed** — no program cleanly grants meaningful credits to a non-commercial individual project. Realistic pilot cost stays ~€20–36/mo. Best €0 shot = apply for the **Founders €1k** anyway (covers ~12mo of a DEV1-M pilot if accepted).
**USER-GATED ACTION (needs your identity/account — I can draft, only you can send):** email `startup-program@scaleway.com` asking (a) does a non-commercial, non-incorporated ed project qualify for the Founders/Early voucher, (b) any academia/education/nonprofit credit track. This is the only way to turn "doubtful" into a fact.

## 🖥️ CORRECTED PRICING + DECISION (2026-07-08) — bare metal added, phased plan chosen
Earlier doc looked ONLY at cloud instances. BARE METAL (fetched 2026-07-08) is the best always-on value:
- **Elastic Metal EM-A116X-SSD: 32 GB RAM, Xeon E3 4C/4T, 2×1TB SSD, €27.99/mo** (Paris PAR-1; +1-month
  one-time commitment fee on monthly billing). Cheaper than cloud DEV1-L (8GB, ~€36 all-in) with 4× the RAM.
- EM-A610R-NVMe: 32 GB, Ryzen 3600 6C/12T, 2×1TB NVMe, €39.99/mo (step-up CPU+NVMe).
- Dedibox Start €4.74/mo BUT 12–36-month lock-in + opaque specs → OUT (kills "upgrade later").
- Cloud (verified): DEV1-S 2GB €6.55 · DEV1-M 4GB €14.74 · BASIC2-A2C-8G 8GB €25.18 · DEV1-XL 12GB €47.50;
  +block ~€0.10/GB/mo (~€2 for 20GB) +IPv4 ~€3.

**DECISION (user 2026-07-08): start CHEAP on CLOUD, upgrade on demand.** Low initial usage → smallest viable,
scale up when it earns it. Cloud (NOT bare metal/Dedibox) BECAUSE cloud resizes in one click, zero commitment.
- **START: DEV1-M (3 vCPU / 4 GB) — €14.74/mo + ~20GB block ~€2 + IPv4 ~€3 ≈ ~€20/mo all-in** (user pick
  2026-07-08). 4 GB comfortably holds the 1.6 GB (disk-resident) corpus + all services with headroom — no
  swap-babysitting during an unmonitored early phase. Rock-bottom alt: DEV1-S (2 GB, ~€12) but tight under any concurrent spike.
- **GRADUATE:** one-click resize to DEV1-L (8 GB) on demand → **bare-metal EM-A116X (32 GB, €28)** when real
  usage / corpus-in-RAM justifies it.
- Provision-time checks: IPv4 inclusion + exact commitment fee on bare metal; live stock; EU region (PAR-1 fine).

## RECOMMENDED setup (one server, shared stack) — SUPERSEDED by the DECISION above (kept for cost detail)
1. **1 instance = DEV1-L (4 vCPU / 8 GB, €31.27/mo)** running ALL Python services (shared engine: sources/VESUM/gates + Hramatka generation + Atlas query + practice API + teacher-app backend). 8 GB comfortably holds the 1.6 GB corpus + services. (DEV1-M €14.74 is the tighter budget option.)
2. **Block storage** ~20 GB for `sources.db` + `atlas.db` ≈ €2/mo.
3. **Public IPv4** ~€3/mo.
4. **DB — start SQLite on the instance** (teacher-accounts + lessons are tiny; matches existing practice/atlas SQLite) → €0 extra. Move to **Managed PG DB-DEV-S (€11.23)** or **Serverless SQL** only when it grows.
5. **Gemma via Google AIS = $0** from the instance (no Scaleway GPU). Key in **Secret Manager**.
6. **nl-ams (EU)** — confirm cheap-DB-tier region availability there.

## Cost picture (verified)
- Minimal pilot (DEV1-M + block + IPv4 + SQLite): **~€20/mo**
- Headroom (DEV1-L + block + IPv4 + SQLite): **~€36/mo**
- With managed PG (DEV1-M + DB-DEV-S + storage + IPv4): **~€31/mo**
- **Potentially €0** on startup/education credits (verify eligibility)
vs HF ~€8/mo (PRO) — BUT HF suits Hramatka-alone/bursty only, NOT an always-on public Atlas+practice
backend with a real DB. So Scaleway is ~€15–30/mo more but does the FULL consolidated EU-resident job
(and credits may zero it). User's "a tiny bit more" ≈ fair in absolute terms.
