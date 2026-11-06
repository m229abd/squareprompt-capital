# Verification record — 2026-11-04

- Final backend Docker image: 31 tests passed; one upstream Starlette/AnyIO deprecation warning.
- Final frontend Docker image: TypeScript and Vite production build passed.
- Compose configuration, including optional Opik profile: valid.
- Workspace, proxied API, MLflow health endpoint and TensorBoard: HTTP 200.
- Synthetic campaign `f9e4321d0c02`: completed 16 experiments, 41 tree nodes, all twelve category branches, an evaluated merge, and a frozen final winner (`h-signals`).
- Final holdout: 96 bars; winner selected before evaluation. Reported returns are synthetic workflow evidence only.
- Pause held persisted step 23 stable. Resume followed by SIGKILL/restart of the worker recovered the campaign and completed it.
- Inner notebook Git: 38 reachable commits, including two-parent merge ancestry.
- All 103 durable telemetry messages delivered; no pending delivery errors. MLflow model `squareprompt-f9e4321d0c02`, version 1, run `26385828f4634563a0944f6b7a67efcd`.
- Browser: final build reloaded, completed campaign and holdout visible; node inspection and campaign wizard verified. Screenshot: `workspace-preview.png`.
- CPU and memory limits added to research services, evaluator, Temporal and PostgreSQL. The idle core stack measured approximately 0.9 GiB after startup; build-time use and campaign peaks differ.

Not verified with this user's credentials: live LLM campaigns, provider-specific billing/reasoning metadata, and end-to-end Opik delivery. The optional Opik stack was not started. Interval refresh logic is implemented but a real multi-hour refresh schedule was not exercised. Live exchange order placement and a full margin/order-book simulator are outside this release. See the README and architecture contract for supported markets and evaluation assumptions.

## Adaptive compute correction

- 36 backend tests passed after replacing the fixed per-candidate cutoff with hardware-calibrated workload selection.
- Tests cover startup/replay separation, faster/slower hardware selecting different workloads, unchanged scoring windows when model cost changes, candidates completing after their target, and termination of child processes that start separate sessions when the total compute allocation is exhausted.
- Real Docker campaign `d8eee8673ae2` completed with a 2-second target and a 120-second total compute budget. Pilot startup alone took 3.79 seconds, so the minimum useful 84-bar workload was selected and estimated at 3.89 seconds. The baseline and two candidate evaluations finished in 4.59, 4.60 and 4.41 seconds without target-based termination. The full 48-bar holdout completed.
- The historical JSON field `evaluation_seconds` is retained for compatibility and now means target duration. Previously completed campaign results remain unchanged.

## Blue spruce and campaign configuration update

- 44 backend tests pass. Frontend TypeScript and production Docker build pass.
- Browser verified blue spruce styling, adding ARB as another sandbox market, independent Developer/Researcher/Critic selections from the configured provider catalog, target-before-data ordering, preservation of an edited 300-bar history when changing target duration, zero-valued resource inputs, quote review, and frozen-cost display. Draft test choices were discarded without saving role defaults or launching paid LLM calls.
- Sandbox campaign `4a7225e0cfd7` completed with DOGE and AAVE, a full 189-bar development window, unlimited USD/tokens/evaluation time, unlimited critic retries, and a finite two-experiment ceiling. Its reviewed execution snapshot `3bfdf163e02d460482d5edb76503cb22` remained pinned. MLflow registered version 1 and telemetry fully drained.
- Live public BTC quote endpoint returned HTTP 200 with exchange taker fee, signed hourly funding, bid/ask and spread. No live LLM calls or exchange orders were submitted by these checks.
- New campaigns hold the reviewed hourly funding rate constant for costs. Previously completed campaigns retain their original results and conventions.
- Screenshot: `blue-spruce-preview.png`.


## Campaign settings refinement — 2026-11-05

- 50 backend tests passed inside Docker. Coverage includes custom/unreported reasoning efforts reaching provider requests, literal `none`, remembered custom efforts, forced catalog refresh replacing cached metadata, the refresh API flag, and required explicit market selections.
- Final Docker frontend build passed TypeScript and Vite. Compose configuration and whitespace checks passed.
- Browser verified fallback suggestions in all three editable reasoning fields (`none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`) and typed a custom Developer effort. No provider calls or saved preference changes were made.
- Sandbox picker fetched 347 perpetual market options from Hyperliquid, with no preselected markets. Both modes use this same fetched catalog component.
- Browser measured all six resource inputs at 195 × 35 pixels, with target evaluation time last and Evaluation data below. Promotional dashboard panels, hero/footer slogans and wizard footer copy were removed. Final screenshot: `campaign-settings-preview.png`.
- Final deployed workspace reloaded successfully after checks. The test draft was discarded; no new campaign or exchange order was submitted.


## ML baseline and fortnightly retraining — 2026-11-05

- 58 backend tests passed in Docker, including actual Jupyter execution. New coverage checks 14 elapsed days across 15m/1h/4h/1d candles, unchanged coefficients between refits, fully matured four-bar labels, insufficient-history flat exposure, invariant scaler/weights/regularization when only holdout prices change, pinned-cost entry bands, a latched drawdown breaker, fitted artifacts and full holdout scoring.
- The bundled baseline now fits ten-feature per-market ridge models. Chronological internal regularization validation purges overlapping four-bar labels. Scalers and models use only the trusted training prefix. No additional host runtime or external ML dependency was introduced.
- Docker campaign `45c9392115e5` completed on synthetic BTC and SOL data with 1,000 hourly bars, a full development target, one candidate and a 240-second total evaluation ceiling. Baseline and candidate each scored 733 portfolio bars and made six fits (three per market). Fit timestamps are separated by exactly 14 days, with coefficients and scalers saved at each update.
- The frozen final winner scored all 200 holdout bars with two initial fits, one per market, restricted to pre-embargo training data. Total campaign evaluation use was about 46 seconds. Synthetic results verify the workflow and do not establish trading profitability.
- The final model artifact is stored in the evaluated inner-Git notebook and MLflow `models.json`; TensorBoard records per-fit details. Scheduled research refresh inputs/API defaults now use 336 hours, separately from model retraining within a replay. No recurring schedule, paid LLM call or exchange order was created by this verification.
- Frontend TypeScript/Vite Docker build, Compose configuration and whitespace checks passed.

## Search workflow and agent controls — 2026-11-05

- 64 backend Docker tests passed; the frontend TypeScript/Vite build and Compose validation passed.
- Browser verified the visible Suggestions menu, fallback levels, an independent pool initialization model, selecting `high` for its reasoning, and typing a custom Developer effort. Draft selections were discarded. Screenshot: `agent-models-preview.png`.
- Sandbox campaign `5532666ddc31` completed four experiments and a full 52-bar holdout, registered MLflow version 1, and drained telemetry. Its actual selection values were 0.910, 0.750, 0.500 and 0.250. The selection record persists through evaluation, and the reservation test confirms that in-flight compute does not inflate the displayed spend or reduce exploration to zero.
- The inner Git graph shows two hypothesis branches forking from the evaluated baseline and two later branches forking from the evaluated sizing notebook. Every branch retains draft and evaluated commits; the winning branch also records holdout. Notebook downloads resolved the recorded revisions. Unit tests separately verify retry ancestry, idempotent commit reuse, immutable blob reads and two-parent merges.
- Sandbox labels identify their edits, including tightening candle-gap tolerance, reducing the volatility sizing target, lowering sleeve exposure and widening the allocation change band. Live initializer routing was tested with a mock provider; verification made no paid LLM calls.
- During verification the API restarted while the Temporal campaign continued to completion. Dashboard polling now projects only displayed curves and dataset metadata before decoding JSON; repeated requests and notebook downloads passed after redeployment. An observed API memory sample dropped from about 401 MiB to 262 MiB under the same 512 MiB limit.

## Follow-up placement and passive benchmarks — 2026-11-05

- 73 backend Docker tests passed, including explicit child/sibling/none placement, immutable follow-up input ancestry, duplicate proposal rejection, finite sandbox variants, comment-only edit detection, benchmark timestamp alignment, signed funding and transaction costs, mixed spot/perpetual treatment, and benchmark caching. The frontend TypeScript/Vite production build and Compose validation passed.
- The repeated zero-gain signal nodes were found in active sandbox campaign `e73700a22a32` (Test run 3). It paused at a durable research boundary after 123 experiments with no in-flight reservation, resumed on the new worker at step 610, and continued with distinct edits and a sibling proposal. Prior nodes and Git artifacts were preserved. No paid provider call was used for verification.
- The existing campaign's Buy & hold and Short & hold series both matched all 205 scored development timestamps. The browser displayed both references alongside Best pipeline and Baseline. Screenshot: `development-benchmarks-preview.png`.

## Temporal and Opik observability — 2026-11-06

- 77 Docker backend tests, the frontend production build, and Compose validation passed.
- Temporal sandbox campaign `a0bd864cfa18` completed with 11 named stage activities. Workflow input contained campaign configuration; evaluation input identified its node, and the activity result contained evaluation metrics and a Git commit. The final result included its winner and 30 scored holdout bars. Existing campaign `316cb6059800` replayed with the updated worker definition.
- The dedicated Opik profile started with healthy dependencies and loopback UI port 5174. The synthetic outbox trace was accepted, read back through the UI proxy, and rendered with its input and output in the browser. ClickHouse's memory ceiling was verified against the trace-list query, not just the health endpoint.
- No paid model calls were made. Screenshots: `opik-trace-preview.png` and `temporal-checkpoints-preview.png`.

## Gateway reliability and history suggestions — 2026-11-06

- Nginx 502s coincided with kernel cgroup OOM kills of the 512 MiB API. The history-default endpoint previously decoded every campaign body. It now extracts only pilot timing fields; the dashboard caches compact cards, selected campaign detail, and dataset metadata. Wizard edits cancel stale requests and dashboard polls do not overlap.
- 79 backend tests passed, followed by nine targeted API tests after the dataset metadata cache change. The frontend production build and Compose validation passed.
- Against the deployed database, 36 mixed dashboard and history-default requests with six clients all returned HTTP 200. The API had zero restarts, OOM events, or memory-limit hits during the check.
- Browser verified the 1,954-bar suggestion for a 15-second target, its development/holdout breakdown, rapid target edits, and the 4,900-bar control. Draft changes were discarded. Screenshot: `history-suggestion-preview.png`.
