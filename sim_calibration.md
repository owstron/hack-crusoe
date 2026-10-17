# Simulator Calibration: Real-World Figures

Collected 2026-10-17 with the You.com Research API. Raw responses with full source lists are in [research/youdotcom/](research/youdotcom/). Use these as **ranges to sample from**, not exact values. The point is "plausible and cited", not "accurate for any one advertiser".

**Source quality:**
- **[A]** peer-reviewed or large academic study
- **[B]** large vendor or industry dataset
- **[C]** practitioner blog (directional only)

Before quoting a number on stage, check the [A] source directly. Sources marked ✓ were checked against the paper's own abstract: Gordon et al. 2022, Lewis & Rao 2015, Braun & Moe 2013. The eBay finding is well known, but we have not re-checked it here.

## 1. Campaign setup

- **Product:** an e-commerce brand (DTC), so a conversion means a purchase.
- **Length:** 4 weeks. **1 checkpoint = 1 week** (4 checkpoints).
- **Monthly budget:** sample $20k-$100k. At ~$40 CPA this gives roughly 100-600 attributed conversions per week: enough to be realistic, small enough that noise matters.
- **Line items (4):** Meta prospecting, Meta retargeting, Google non-brand search, Google brand search.

Brand search gives us a second over-credit trap on Google. That provides the "trap variation" for test campaigns (over-credit on Google instead of Meta) without inventing anything.

## 2. What agents see: attributed (last-click) metrics

| Figure | Value | Source |
|---|---|---|
| Meta e-commerce CPM | median $15.06 (range ~$11.50-$21.80 by vertical) | [B] Triple Whale, 40k+ brands, Aug 2025-Jul 2026 |
| Meta e-commerce CTR | 2.39% median (1.9%-3.0%) | [B] Triple Whale |
| Meta e-commerce conversion rate | 1.53% median (1.1%-1.9%) | [B] Triple Whale |
| Meta e-commerce CPA | $38.99 median ($27-$52 by vertical) | [B] Triple Whale |
| Google search CTR | 6.66% | [B] WordStream/LocaliQ, 16,446 US campaigns, 2024-25 |
| Google search CPC | $5.26 (2026 edition: $5.42) | [B] WordStream/LocaliQ |
| Google search conversion rate | 7.52% (2026: 8.18%); mostly leads, so lower for e-commerce purchases | [B] WordStream/LocaliQ |
| Google search CPA/CPL | $70.11 (2026: $66.69) | [B] WordStream/LocaliQ |

**Simulator setting: attributed CPA ranges per line item** (sample per campaign):

| Line item | Attributed CPA range | Reasoning |
|---|---|---|
| Meta prospecting | $35-$60 | Around the Triple Whale median |
| Meta retargeting | $10-$20 | Warm audience; looks 2-4× cheaper than prospecting |
| Google non-brand search | $50-$90 | Around the WordStream CPA |
| Google brand search | $8-$20 | People already searching for the brand; very cheap last-click |

The retargeting and brand ranges are our assumption, consistent with the sources' descriptions of these channels "looking" far better than they are. No source gives a single benchmark.

**Simulator setting: delivery metrics** (added 2026-10-17 so each weekly row matches a Meta Insights / Google Ads row: impressions, reach, frequency, clicks, CTR, CPM, CPC, purchases, cost per purchase, purchase value, ROAS):

| Line item | Setting | Basis |
|---|---|---|
| Meta prospecting | CPM $11.50-$21.80, CTR 1.9-3.0% | [B] Triple Whale ranges |
| Meta retargeting | CPM $15-$30, CTR 2.0-3.5% | Our assumption: warm audiences cost more and click more |
| Google non-brand | CPC $3.50-$7.50, CTR 4.5-8.5% | [B] WordStream ($5.26, 6.66%) |
| Google brand | CPC $1-$3, CTR 15-30% | Our assumption: brand terms are cheap and high-intent |

- **Attribution label:** Meta rows are `7d_click_1d_view` (Meta's default attribution setting, verified on our ad account's Insights API 2026-10-17); Google rows are `last_click`.
- **Frequency:** each Meta line item has an audience sized so weekly frequency is ~1.58 at half-saturation spend K; reach = N·(1 − e^(−impressions/N)). Small-K line items (retargeting) hit frequency 3-4 when over-funded.
- **CTR** falls with creative age (same decay as conversions) and with frequency above 1.58, as frequency^−0.43 ([B] Meta creative fatigue analysis, section 5).
- **Conversion rate is not set directly**; it is conversions / clicks. Check across 60 campaigns at an equal budget split (week 1 medians): Meta prospecting CPM $15.62, CTR 2.49%, CPA $34, conversion rate 2.0% (benchmark 1.1-1.9%: slightly high); Google non-brand CPC $5.08, CTR 6.72%, conversion rate 8.0%, CPA $61 (benchmarks $5.26, 6.66%, 7.5-8.2%, $66-70).
- Average order value per campaign: $60-$120, so purchase value and ROAS are reported too.
- These metrics use a separate random stream: adding them did not change conversions, holdout results or any score.

**Not modeled (say so if asked):** Meta reports lift through separate Conversion Lift studies with minimum sizes and end-of-study results, not a weekly running holdout like ours; and recent conversions keep arriving for up to 7 days (see the optional conversion-lag trap in section 6).

## 3. Hidden truth: what share of attributed conversions are incremental

This is the core ground truth. `incrementality = true incremental conversions / last-click attributed conversions`.

| Line item | Simulator range | Evidence |
|---|---|---|
| **Meta retargeting** | **0.08-0.30** | [A] Google Display ghost-ad meta-study, 432 experiments: 8% median lift (≈7.4% of exposed conversions incremental). [A/B] retailer retargeting ghost ads: 10.5% lift (≈9.5%). [B] Criteo holdouts: ~8% revenue lift. [C] Industry syntheses: 20-40%, median 28%. |
| **Google brand search** | **0.00-0.25** | [A] eBay field experiment (Blake, Nosko & Tadelis, *Econometrica* 2015): 99.5% of paid brand clicks recovered organically, no measurable sales effect. [B] 225 geo tests: brand search median iROAS 0.70×. [C] Practitioner test: 19% incrementality. [A] Counterexample: Edmunds kept only ~50% via organic, so it varies with brand strength. |
| **Google non-brand search** | **0.40-0.65** | [A] eBay non-brand: ~42% of paid clicks incremental. [C] Practitioner test: 59% incrementality factor. [B] Geo tests: median iROAS 1.86×. |
| **Meta prospecting** | **0.85-1.20** | [B] Haus, 640 Meta experiments: Meta's 7-day-click reporting **under-reported** incremental impact by 15% on average. [A] Gordon et al., 663 Facebook RCTs: median lift 29% upper-funnel, 18% mid-funnel, 5% lower-funnel. |

**Two things to notice:**
- Prospecting can be **under-credited** by last-click, as well as retargeting being over-credited. That gives a mirror-image trap: the line item that looks worst may be the best.
- Gordon et al. also found that observational methods **could not reliably estimate** causal effects. That is why our agents need holdout tests, not last-click data. Quote it in the pitch: it's the real-world reason this project exists.

## 4. Diminishing returns

**Evidence that it happens:**

| Study | Finding |
|---|---|
| [A] Johnson, Lewis & Reiley, "Add More Ads?" (randomized, 3M+ Yahoo users) | Returns roughly linear up to ~50 impressions per user. Beyond that, marginal value per impression fell from ~$0.037 to ~$0.01 or less |
| [A] Försch & de Haan 2018, *IJRM* (5.8B impressions, 158 advertisers) | Higher frequency means lower CTR, more so for high-spending advertisers |
| [A] Chae, Bruno & Feinberg 2019, *JMR* | ~24% of users became "weary": more ads *reduced* visits |
| [B] Lewis 2014 working paper (30 Yahoo campaigns, 2.8B impressions) | 67% of campaigns showed wear-out with frequency; none showed increasing returns |

**Honest caveat:** peer-reviewed evidence is about **frequency** (the same people seeing more ads), not dollars. Spend curves are rarely measured directly because changing spend also changes targeting and reach. Our model connects the two: retargeting and brand search reach a **small, fixed audience**, so extra spend mostly means higher frequency. That is why those line items get a small K (below). The Hill curve itself is standard practice in marketing mix models (Google Meridian, Meta Robyn), not a measured fact.

**How we model it:** marketing mix models (Google Meridian, Meta Robyn) model diminishing returns with a **Hill curve**:

```
response(spend) = max_lift × spend^S / (spend^S + K^S)
```

- `K` = half-saturation spend (response is 50% of max at `K`).
- `S` = shape. `S ≤ 1` means diminishing from the first dollar; `S > 1` is S-shaped.
- [A/B] Robyn searches S over ~0.5-3; Google's Bayesian MMM paper uses a prior with mean 3. Practitioners use 1-3.
- **There is no universal dollar value for K.** It depends on market size and channel ([B] Meridian docs). So K is our choice, set relative to budget.

**Simulator settings:**

| Line item | S | K (as share of total monthly budget) | Reasoning |
|---|---|---|---|
| Meta prospecting | 1.0-1.5 | 0.4-0.8 | Large audience, more headroom |
| Meta retargeting | 1.0 | 0.05-0.15 | Small audience saturates fast |
| Google non-brand | 1.0 | 0.2-0.5 | Limited by search volume |
| Google brand | 1.0 | 0.03-0.10 | Brand search volume is small and fixed |

Use S = 1 by default. Its concave curve makes the best-budget-split calculation a simple greedy loop. Only use S > 1 if you want an extra trap where spending too little looks useless.

Optional, skip unless ahead of schedule: **adstock**, where ad effects carry over to later weeks. [B] Typical weekly carryover is 0.1-0.4 for search and 0.3-0.5 for social.

## 5. Creative fatigue

**Academic evidence:**

| Study | Finding |
|---|---|
| ✓ [A] Braun & Moe 2013, *Marketing Science* 32(5) (individual-level online display data) | Models ad wear-out and restoration across multiple creatives. Varying creative by each person's impression history: **+12.7% visits, +13.8% conversions**. Retention per repeated exposure (from the paper body, via You.com): ~0.78 per exposure for the campaign overall, ~0.40 when the *same creative* repeats |
| [A] Schmidt & Eisend 2015, *Journal of Advertising* (meta-analysis, 312 effect sizes) | Attitude toward the ad peaks around 10 exposures, then declines |
| [A] Chae, Bruno & Feinberg 2019, *JMR* | For the weary 24% of users, response turned negative after about 3 impressions within 2 days |
| [A] Pechmann & Stewart 1988 (review) | Classic "wear-in then wear-out" inverted-U pattern |

**Practitioner figures** (used only to set weekly decay rates):

| Figure | Value | Source |
|---|---|---|
| Meta click likelihood vs. prior exposures | ≈ (N+1)^−0.43 | [B] Meta Analytics blog on creative fatigue |
| Meta conversion likelihood by 4th exposure | ~45% lower | [C] widely cited, directional |
| Fresh creative added to fatigued ad sets | ~8% average conversion-rate lift | [B] Meta Analytics blog |
| Meta retargeting fatigue onset | 3-7 days | [C] practitioner benchmarks |
| Meta prospecting fatigue onset | 10-21 days | [C] |
| Refresh trigger | sustained ≥20% CTR decline | [C] |

**Simulator settings** (decay of true incremental response per weekly checkpoint since the last creative rotation):

| Line item | Decay per checkpoint |
|---|---|
| Meta retargeting | 15-30% |
| Meta prospecting | 5-15% |
| Google search (both) | 0-5% (search ads are text and intent-driven; our assumption) |

`rotate_creative` resets the fatigue clock. Optionally it costs a 1-checkpoint "learning" dip of ~10%.

## 6. Noise and holdout tests

**Academic evidence:**

| Study | Finding |
|---|---|
| ✓ [A] Lewis & Rao 2015, *Quarterly Journal of Economics* 130(4) (25 field experiments, $2.8M spend) | Measuring ad returns is hard. The median confidence interval on ROI is **over 100 percentage points wide**. Individual sales are very volatile (coefficient of variation ~10). Informative experiments can need **over 10 million person-weeks** |
| [A] Johnson, Lewis & Reiley 2017, *Marketing Science* (3M users) | An earlier comparable experiment had only ~47% power |
| [A] Google Display ghost-ad study (432 experiments) | The median experiment needed ~3.3M exposed users just to tell break-even from ineffective |

So noisy, often-inconclusive tests are the real-world norm, not something we invented. Our simulator is far *less* noisy than reality, which keeps the 4-checkpoint demo learnable.

**Rules of thumb:**

| Figure | Value | Source |
|---|---|---|
| Conversion count noise | Poisson: relative noise ≈ 1/√(conversions). 25/day → 20%, 100/day → 10% | [A] standard statistics |
| Real data is noisier than Poisson | Overdispersed (weekday, auction, seasonality) | [B] |
| Conversion lag | 40-60% of conversions visible same day; 95-98% after 4-7 days | [B] Google Ads Help, Shopping example |
| Lift test sample size (α=0.05, 80% power) | conversions per arm ≈ 15.68 / (relative lift)². 20% lift → ~400/arm; 10% → ~1,600/arm | [A] standard power calculation |
| Typical test duration | 2-4 weeks for high-volume accounts | [B] Optimizely |

**Simulator settings:**
- Observed conversions ~ **negative binomial** (Poisson with extra variance) around the expected value. Start with variance ≈ 1.5 × mean.
- **Holdout test:** the agent picks a holdout share (e.g. 10-20%) for a line item. Those users see no ads, so it costs that share of true incremental conversions. The simulator returns measured lift with noise from the sample-size formula above. With our weekly volumes, small line items give **inconclusive** results. That is realistic, and it is the small-sample trap.
- Optional extra trap: report the latest checkpoint's conversions at ~70% of their final value (conversion lag), so the latest week always looks worse than it is.

## 7. Summary: what each trap is based on

Every trap is a **setting value** in one of 4 simulator mechanisms: incrementality ratio, saturation curve, fatigue decay, and noise. Adding a trap means choosing settings, not writing new code.

| # | Trap | Mechanism and setting | Primary research | Strength |
|---|---|---|---|---|
| 1 | Retargeting over-credit | incrementality 0.08-0.30 | [A] Google ghost-ad study, 432 experiments (8% median lift); [B] Criteo holdouts (~8%) | Strong |
| 2 | Brand search over-credit (test-set variation of #1) | incrementality 0.00-0.25 | [A] Blake, Nosko & Tadelis 2015, *Econometrica* (eBay: 99.5% of brand clicks recovered organically) | Strong |
| 3 | Prospecting under-credit | incrementality 0.85-1.20 | [B] Haus, 640 Meta experiments (last-click under-reported by 15%); ✓ [A] Gordon et al., 663 Facebook RCTs (29% upper-funnel median lift) | Medium: the main number is from an industry vendor |
| 4 | Diminishing returns | Hill K small for retargeting and brand | [A] Johnson, Lewis & Reiley (flattening past ~50 impressions); [A] Försch & de Haan 2018; Hill curve from Meridian/Robyn | Medium: evidence is about frequency, not spend (see section 4) |
| 5 | Creative fatigue | 15-30%/week decay for retargeting; rotation resets | ✓ [A] Braun & Moe 2013 (+13.8% conversions from varying creative); [A] Schmidt & Eisend 2015 meta-analysis | Strong that it exists. Weekly rates are practitioner figures [C] |
| 6 | Small-sample noise | negative binomial; holdout power formula | ✓ [A] Lewis & Rao 2015, *QJE* (median ROI confidence interval >100 points wide) | Strong |

## 8. How to cite in the demo and README

> "Every trap in the simulator is based on published research: 663 Facebook RCTs (Gordon et al.), Google's 432-experiment ghost-ad study, eBay's paid search experiment (Blake, Nosko & Tadelis, *Econometrica* 2015), Lewis & Rao on measurement noise (*QJE* 2015), and Braun & Moe on creative wear-out (*Marketing Science* 2013), plus industry benchmarks from Triple Whale and WordStream/LocaliQ. We researched them with You.com."
