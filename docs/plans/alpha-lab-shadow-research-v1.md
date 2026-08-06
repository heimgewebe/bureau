# Alpha Lab Bounded Form-4 Kill Test V1

Alpha Lab is a bounded secondary experiment, not the primary Schotter path. It remains research-only: public SEC reads and separately approved free market, FX, calendar and corporate-action observations; no broker, wallet, order API, leverage or capital deployment.

## Decision

The former programme was too large. A general historical research platform followed by at least 200 prospective signals and six months of operation is not justified before a cheap test establishes that any robust public signal survives timing, costs and simple baselines.

The remaining commitment is exactly one falsification kill test.

## Sequence

1. T001 preserves the already merged lawful point-in-time evidence foundation.
2. T002 preregisters and executes the single kill test below.
3. T003 is superseded. No prospective collector, paper broker or six-month cohort is preauthorised.
4. A positive T002 outcome creates only an independent-review request. Any further experiment requires a new task and a fresh opportunity-cost decision.

## Fixed hypothesis

Publicly disclosed original Form 4 non-derivative open-market purchases may have positive subsequent returns after the information is public and after conservative execution costs.

The test includes only events that satisfy all of these conditions:

- original Form 4 rather than an amendment,
- non-derivative transaction,
- transaction code `P`,
- acquired rather than disposed shares,
- explicit non-10b5-1 status under the parser contract,
- valid public filing acceptance timestamp,
- listed common stock with the fixed liquidity rule,
- complete point-in-time price, calendar, FX and corporate-action coverage.

No narrative, LLM, sentiment, insider-ranking or adaptive filter is added.

## Frozen execution rule

- Signal time: public filing acceptance time, never transaction date.
- Entry: official close of the next eligible trading day after public acceptance.
- Exit: official close exactly 20 eligible trading days after entry.
- Independence: at most one event per issuer in each 20-trading-day holding window; later overlapping events are aggregated or rejected and never counted as independent observations.
- Primary round-trip cost: 50 basis points.
- Cost stress: 100 basis points.
- No stop loss, take profit, dynamic holding period, averaging down or post-outcome exclusion.

## Universe and period rule

The eligible universe and liquidity threshold must be frozen before any outcome is read. The chosen threshold must be computable from the same lawful free point-in-time source used by the test and must exclude instruments whose simulated execution is not credible.

The test period is selected only from source coverage metadata:

- use the longest contiguous period jointly covered by all required approved sources,
- do not inspect returns when choosing the period,
- reserve the final 20 percent of chronological events as the untouched final segment,
- require at least 200 issuer-independent eligible events after all exclusions,
- otherwise terminate immediately with no-go.

## Fixed comparisons

The untouched final segment is compared with:

1. equal-weight eligible-universe returns over the same holding windows,
2. one simple preregistered momentum baseline,
3. matched random controls using only frozen non-outcome attributes such as price and liquidity,
4. all otherwise eligible Form 4 purchases without additional ranking.

The implementation may calculate descriptive development views, but no rule may be selected because it looks best.

## Mandatory stress views

The final report includes:

- primary 50-basis-point round-trip cost,
- doubled 100-basis-point cost,
- removal of the ten best strategy trades,
- year or broad market-regime slices defined before results,
- contribution concentration by issuer and liquidity bucket,
- the fixed eligible-universe, momentum and matched-random comparisons.

Overlapping issuer events are not independent observations. Significance or confidence claims must use the issuer-independent sample.

## Automatic no-go gates

T002 ends with no-go when any of these conditions holds:

1. no lawful free source set provides complete required coverage,
2. fewer than 200 independent eligible events remain,
3. public filing acceptance times cannot be reconstructed reliably,
4. the effect disappears with next-trading-day-close entry,
5. the effect disappears after the primary cost model,
6. the effect disappears under doubled costs,
7. the result no longer beats fixed baselines,
8. removing the ten best trades removes the effect,
9. a few illiquid securities or issuers dominate the result,
10. missing delistings, corporate actions, calendars or FX make the final segment non-reproducible.

No failed gate may be repaired by changing the protocol after outcomes are visible.

## Resource cap

T002 may add only what the fixed kill test requires. It must not add:

- paid data or a subscription,
- scheduled prospective collection,
- a general strategy-optimisation framework,
- ML or LLM ranking,
- broker, wallet or order integration,
- paper trading,
- options, CFDs, leverage or shorting,
- a second strategy after the first fails.

If free lawful data is unavailable, the correct product is a short no-go report, not more infrastructure.

## Required output

T002 publishes:

- canonical preregistration JSON and digest created before outcomes,
- source and license matrix digest,
- content-addressed input and configuration manifests,
- complete positive or negative final report,
- all mandatory stress views,
- one terminal classification: `no-go` or `independent-review-request`.

Neither classification authorises prospective shadow operation or capital. A later decision must compare any apparent edge with the opportunity cost of direct paid work.

## Evidence and current boundary

- Alpha Lab data foundation merge: `4e5a11426fde8733059a680704da69082999f84b`.
- Python 3.10 and 3.12 PR checks were green; the merge-commit push CI was green.
- Existing synthetic 200 EUR example ended at 199.87 EUR after costs and is not market evidence.
- T001 closeout and queue publication are handled by a separate owner and are not modified by this scope revision.
- No task authorises paid data, real orders, broker or wallet access, leverage, CFDs, options, shorting or automatic progression to capital.
