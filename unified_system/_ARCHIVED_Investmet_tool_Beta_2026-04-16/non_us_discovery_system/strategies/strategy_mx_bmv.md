# Strategy — Mexico BMV

**Exchange:** Bolsa Mexicana de Valores (BMV). Secondary Mexican exchange BIVA trades the same issuers.
**MIC:** XMEX (BMV)
**Language:** Spanish. Some large-cap issuers file English translations voluntarily (particularly those with NYSE ADRs).
**Translation required:** Yes — in-session Claude translation with confidence scoring per D-002.
**Build phase:** 9 (final)
**Status:** STUB — to flesh out in Phase 9 after Brazil is stable.

---

## 1. Data source

BMV and CNBV (Comisión Nacional Bancaria y de Valores — Mexico's securities regulator) host the disclosure infrastructure. STIV-2 (Sistema de Transferencia de Información sobre Valores) is the statutory filing system.

**Planned primary endpoints (UNVERIFIED — probe at Phase 9):**

- BMV: `https://www.bmv.com.mx/es/emisoras/` — issuer page with filing history.
- CNBV EMISNET / STIV-2: `https://www.bmv.com.mx/es/grupo-bmv/sistemas-electronicos-sif-icap`.
- Event disclosures: `https://www.bmv.com.mx/es/mercados/listado-de-valores`.

## 2. Filing categories of interest

| CNBV / BMV filing type | Signal type |
|------------------------|-------------|
| Evento Relevante (Material Event) | `evento_relevante` |
| Información Financiera Trimestral (quarterly financials) | `quarterly_financials` |
| Información Financiera Anual (annual financials) | `annual_financials` |
| OPA (Oferta Pública de Adquisición — tender offer) | `opa_tender_offer` |
| Reestructura corporativa (corporate restructuring) | `corporate_restructure` |
| Cambio de control (change of control) | `change_of_control` |
| 10%+ tenencia accionaria (major shareholder report) | `major_shareholder_change` |
| Recompra de acciones (share buyback) | `buyback_announcement` |
| Suspensión de cotización (trading suspension) | `trading_suspension` |
| Reporte de administración (management report) | `management_report` |

## 3. Signal filters (Stage 1 triage)

- BMV main board only.
- Ticker + `.MX` resolves via yfinance (note: yfinance coverage of Mexican equities is weaker than other markets — fall back to Bloomberg/FIGI lookup when yfinance returns nothing).
- Market cap ≥ USD $300M. Mexico's listed universe is relatively small (~150 investable names at this floor); scanner tolerates lower candidate volume.
- Series-aware: Mexican issuers often have multiple series (Series A, B, L, etc.) with different voting rights. Scanner normalizes to primary trading series.

## 4. Entity resolution (D-003)

OpenFIGI: `{"idType": "TICKER", "idValue": "<ticker>", "micCode": "XMEX"}`.

Cross-listing awareness:
- NYSE ADRs: América Móvil, Cemex, Grupo Televisa, FEMSA, Grupo Bimbo, Kimberly-Clark de Mexico. Flag `cross_listed_on: ["XNYS"]`.
- SIC (Sistema Internacional de Cotizaciones): BMV's international listing segment hosts hundreds of foreign tickers. Scanner must exclude SIC — it's not Mexican issuer disclosure.

## 5. Translation integrity

Per D-002. Spanish critical flip-error phrases:
- aumento / disminución (increase / decrease)
- superior a / inferior a (above / below)
- previsto / esperado (projected / expected)
- no (negation, multiple positions)
- mayor que / menor que (greater than / less than)

Mexican Spanish differs from Iberian Spanish in regulatory vocabulary — scanner uses es-MX glossary.

## 6. Signal output

Standard schema. `company_name_local` = Spanish name. `raw_data.cnbv_category` captures the specific event category.

## 7. Deep dive checklist

- For Eventos Relevantes: Mexican continuous-disclosure culture has improved post-2018 reforms but remains inconsistent. Parse carefully.
- For OPAs: Mexican tender offer rules (LMV — Ley del Mercado de Valores) require specific minority protections; check compliance.
- For change-of-control: CNBV approval process, Cofece (antitrust) filings where relevant.
- For buybacks: Mexican accounting treatment and tax implications differ from US.
- Web research layer: El Financiero, Reforma, Bloomberg Línea, Expansión, Reuters Mexico.
- Macro overlay: MXN FX, US-Mexico trade relationship, USMCA compliance all drive thesis-relevant risk.

## 8. Known risks

- **Thin investable universe.** ~150 names at $300M+ means candidate flow will be lowest among the 9 exchanges. Expect maybe 1–3 signals per scan.
- **Concentration in conglomerates.** Grupo Carso, FEMSA, Alfa, Grupo México — complex cross-ownership adds diligence cost.
- **Political/fiscal cycle risk.** Mexican presidential cycles (6-year sexenio) produce large policy swings affecting specific sectors (energy, infrastructure, banking).
- **FX dominance.** MXN moves often swamp security-specific alpha. Factor into Risk/Reward.

## 9. Tool file

`tools/bmv_scanner.py` — Phase 9.
