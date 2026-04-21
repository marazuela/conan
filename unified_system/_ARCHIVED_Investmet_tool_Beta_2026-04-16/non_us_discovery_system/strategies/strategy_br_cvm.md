# Strategy — Brazil CVM / B3

**Exchange:** B3 (formerly BM&FBOVESPA) — São Paulo
**MIC:** BVMF
**Language:** Portuguese (Brazilian). Some large-cap issuers file English translations voluntarily.
**Translation required:** Yes — in-session Claude translation with confidence scoring per D-002.
**Build phase:** 8
**Status:** STUB — to flesh out in Phase 8 after India is stable.

---

## 1. Data source

CVM (Comissão de Valores Mobiliários) is Brazil's SEC-equivalent. Listed issuers file structured disclosures through the CVM portal and B3 simultaneously.

**Planned primary endpoints (UNVERIFIED — probe at Phase 8):**

- CVM portal: `https://www.rad.cvm.gov.br/ENET/frmConsultaExternaCVM.aspx` — public filing search.
- B3: `https://www.b3.com.br/en_us/market-data-and-indices/data-services/market-data/consultations/listed-companies/listed-companies/` — company filings.
- IPE (Informações Periódicas e Eventuais) system on CVM — structured feeds.

## 2. Filing categories of interest

| CVM / B3 filing type | Signal type |
|----------------------|-------------|
| Fato Relevante (Material Fact, CVM Instruction 358) | `fato_relevante` |
| Comunicado ao Mercado (Market Communication) | `market_communication` |
| Formulário de Referência (annual reference form) | `reference_form_update` |
| ITR (quarterly financial statements) | `quarterly_itr` |
| DFP (annual financial statements) | `annual_dfp` |
| OPA (Oferta Pública de Aquisição — tender offer) | `opa_tender_offer` |
| Acordo de Acionistas (shareholders' agreement change) | `shareholders_agreement` |
| 5%+ shareholder notification | `major_shareholder_change` |
| Incorporação / Cisão (merger / spin-off) | `merger_spinoff` |
| Recuperação Judicial (judicial recovery — bankruptcy) | `judicial_recovery` |

## 3. Signal filters (Stage 1 triage)

- Novo Mercado, Level 2, Level 1 listing segments preferred (best governance); Traditional segment accepted with caution.
- Ticker + `.SA` resolves via yfinance.
- Market cap ≥ USD $300M (Brazil's filter will be hit by many mid-caps given BRL weakness — the $300M USD floor is still enforced).
- Avoid Units (paired common+preferred) unless the filing is Unit-specific; scanner normalizes to common share ticker where possible.

## 4. Entity resolution (D-003)

OpenFIGI: `{"idType": "TICKER", "idValue": "<ticker>", "micCode": "BVMF"}`.

Cross-listing awareness: many Brazilian issuers have NYSE ADRs (Vale, Petrobras, Itaú, Ambev). Flag `cross_listed_on: ["XNYS"]`.

## 5. Translation integrity

Per D-002. Portuguese critical flip-error phrases:
- aumento / redução (increase / decrease)
- acima / abaixo (above / below)
- previsto / esperado (projected / expected)
- não (negation)
- superior a / inferior a (higher than / lower than)

Brazilian Portuguese differs from European Portuguese in regulatory vocabulary — scanner uses pt-BR-specific glossary.

## 6. Signal output

Standard schema. `company_name_local` = Portuguese name. `raw_data.cvm_category` captures the specific IPE category code.

## 7. Deep dive checklist

- For Fatos Relevantes: these are the highest-materiality category — mandatory disclosure for any information that could affect price. Parse carefully; many are mundane, but structurally significant ones (M&A, guidance, operational catastrophe) are flagged explicitly.
- For OPAs: offer structure (unified OPA vs. fragmented), tag-along rights (Novo Mercado = 100% tag-along on control change).
- For judicial recovery (RJ): plan vote date, creditor class composition, debtor-in-possession financing status.
- Web research layer: Valor Econômico, Folha de S.Paulo, Estadão, Reuters Brazil, Brazil Journal.
- FX overlay: BRL volatility can dominate USD returns; factor into Risk/Reward.

## 8. Known risks

- **Political cycle effects.** Brazilian equities are highly sensitive to fiscal/political news (Planalto, Congress, Central Bank independence). Deep-dive must include current political overlay at scan time.
- **State-owned enterprise governance.** Petrobras, Banco do Brasil subject to government-appointed boards — governance reversal is a recurring risk.
- **Tag-along and tender offer mechanics** differ by listing segment — scanner must record segment.
- **BRL FX exposure** means USD-denominated returns can diverge sharply from local-currency performance.

## 9. Tool file

`tools/cvm_scanner.py` — Phase 8.
