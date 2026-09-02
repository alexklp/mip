# МІП — Relation Candidate Profile checkpoint (02.09.2026)

## Контекст

Після повного догону claim embeddings (`29912/29912`, missing=0) старий `candidate_pair_generator.py` не запускали на повному corpus через його full-matrix + materialize-all-pairs реалізацію. Замість цього додано read-only blockwise profiler `experiments/claim_relations/candidate_pair_profile.py`, який зберігає source-provenance cross-group семантику `candidate_version=1`, але не пише у `candidate_pairs`.

Цей checkpoint supersedes припущення про те, що full-corpus exact profiling на ~30k claims може бути дорогим сам по собі: фактичний blockwise NumPy scan виявився дуже дешевим.

## Фактичний corpus

Source-group sets для claims з embeddings:

- `{2}`: 150 claims
- `{3}`: 8,800 claims
- `{4}`: 20,962 claims
- total: 29,912 claims

Eligible disjoint source-group pair space:

- `{2} x {3}`: 1,320,000
- `{2} x {4}`: 3,144,300
- `{3} x {4}`: 184,465,600
- total: **188,929,900 pairs**

## Performance result

Run parameters:

- chunk size: 1000
- near-duplicate sample size: 20
- read-only; DB connection closed before matrix profiling

Measured:

- profiler compute elapsed: **1.71 s**
- total wall clock including startup/DB reads: **4.60 s**
- user CPU: 35.48 s
- system CPU: 0.56 s
- CPU utilization: 782%
- peak RSS: **778,444 KB (~760 MiB)**
- swaps: 0
- exit status: 0

Conclusion: at current ~30k scale exact blockwise NumPy profiling is operationally trivial. HNSW/ANN is not justified by performance need for this stage.

## Cosine distribution across all 188,929,900 eligible pairs

- `<0.50`: 186,593,407
- `[0.50,0.55)`: 1,625,144
- `[0.55,0.60)`: 495,701
- `[0.60,0.65)`: 145,418
- `[0.65,0.70)`: 44,753
- `[0.70,0.75)`: 15,416
- `[0.75,0.80)`: 5,946
- `[0.80,0.85)`: 2,588
- `[0.85,0.90)`: 1,007
- `[0.90,0.95)`: 395
- `[0.95,1.00]`: 125

Counts above thresholds:

- `>=0.80`: 4,115 (0.002178%)
- `>=0.85`: 1,527 (0.000808%)
- `>=0.90`: 520 (0.000275%)
- `>=0.95`: 125 (0.000066%)
- `>=0.97`: 61 (0.000032%)

The high-similarity tail is extremely sparse; candidate generation does not need ANN just to reduce the pair space at this corpus size.

## Coverage / degree under current cross-source-group eligibility

At `score >= 0.80`:

- zero-degree claims: 28,352 / 29,912 = 94.78%
- nonzero coverage: 1,560 claims = ~5.22%
- median degree=0, p90=0, p99=5, max=82

At `score >= 0.85`:

- zero-degree claims: 29,115 / 29,912 = 97.34%
- nonzero coverage: 797 claims = ~2.66%
- median degree=0, p90=0, p99=2, max=39

At `score >= 0.90`:

- zero-degree claims: 29,556 / 29,912 = 98.81%
- nonzero coverage: 356 claims = ~1.19%
- median degree=0, p90=0, p99=1, max=18

Measured hubs are mostly generic casualty phrases (`Є постраждалі`, `Есть пострадавшие`, `Є поранені`, `Есть раненые`, `Десятки людей поранено`). This empirically confirms that raw global cosine ranking can concentrate relation budget on generic/template language rather than broad graph coverage.

## Time distribution for score >= 0.85

Among 1,527 pairs:

- within 1 day: 560 (36.67%)
- within 3 days: 968 (63.39%)
- within 7 days: 1,312 (85.92%)
- within 14 days: 1,508 (98.76%)

This makes a bounded time window a strong v2 hypothesis, but **no window is frozen yet**. `first_seen` is observed first collection time, and may not be sufficient for resurfaced/repeated historical material; latest/relevant occurrence semantics still require review before becoming contract.

## Near-duplicate band >= 0.97

Total pairs: 61.

Sample contains both:

1. genuine near-duplicate / translation / same-fact pairs, e.g. nearly identical NABU/SAP statements, casualty counts, translated headlines;
2. generic phrases that repeat across unrelated events and large time gaps, e.g. `Є постраждалі` vs `Есть пострадавшие`, including gaps of ~95h, ~143h and ~293h.

Therefore a high cosine threshold alone is not a semantic relation gate. It is useful as recall/ranking evidence, but generic text can create high-degree hubs and false semantic proximity.

## Decisions after measurement

1. Do **not** run the old materialize-all-pairs generator on the full corpus.
2. Exact blockwise NumPy remains the reference computation at current scale; HNSW is not needed by measured performance.
3. `candidate_version=1` remains a historical/reference source-provenance cross-group contract, not yet the final live relation candidate policy.
4. Do not freeze v2 thresholds/window/top-k from intuition. First inspect a stratified semantic sample across score bands.
5. Next deterministic step: read-only stratified sampling for bands `0.80-0.85`, `0.85-0.90`, `0.90-0.95`, `0.95-0.97`, `>=0.97`; then decide which subset is worth Mamay relation-judgment calibration.
6. Broader architectural question remains open: if evidence graph is for general canonical event construction rather than only cross-source-group narrative comparison, cross-group eligibility may be too restrictive because measured graph coverage is very low. This is not changed yet; it requires explicit design decision after semantic sampling.

## Numerical safeguard

Block scores are clipped to `[-1,1]` before statistics because normalized float32 dot products can numerically exceed mathematical cosine bounds by tiny rounding error. This matches the DDL `score BETWEEN -1 AND 1` domain and prevents a future exact-duplicate pair from failing persistence purely on float32 noise.
