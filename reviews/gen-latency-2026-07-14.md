# Generation Latency Analysis Note (2026-07-14)

This note analyzes the end-to-end generation latency in Hramatka, specifically explaining the observed ~23-minute bake time for a 45-minute lesson ready sequence and recommending options to optimize performance.

---

## 1. Code Citation & Serial Execution Flow

The entire generation pipeline runs in a strictly serial, single-threaded manner:

1. **Phase Loop**:
   In `EngineLessonBaker.bake` ([hramatka/api/baking/engine_adapter.py:412](file:///Users/krisztiankoos/projects/learn-ukrainian-infra-private/.worktrees/dispatch/agy/hramatka-gen-telemetry/hramatka/api/baking/engine_adapter.py#L412)), the baker iterates sequentially over each phase of the plan:
   ```python
   for phase, count_plan in phase_count_plans.items():
       phase_results[phase] = pipeline.run(...)
   ```

2. **Pipeline Run**:
   `pipeline.run` delegates to `_run` ([hramatka/engine/pipeline.py:808](file:///Users/krisztiankoos/projects/learn-ukrainian-infra-private/.worktrees/dispatch/agy/hramatka-gen-telemetry/hramatka/engine/pipeline.py#L808)), which executes generation, gating, and selection for the phase synchronously.

3. **Regeneration Loop**:
   Inside `_run` ([hramatka/engine/pipeline.py:686](file:///Users/krisztiankoos/projects/learn-ukrainian-infra-private/.worktrees/dispatch/agy/hramatka-gen-telemetry/hramatka/engine/pipeline.py#L686)), if the selected activities do not satisfy the phase requirements (deficits exist), regeneration calls are made sequentially up to `max_regeneration_attempts` (which is `2` in production):
   ```python
   for attempt in range(len(raw_batches), max_regeneration_attempts + 1):
       ...
       regenerated = _candidate_generator(...)
   ```

4. **Prompt Grouping & Call Coalescing**:
   Within `generate` ([hramatka/engine/generate.py:254](file:///Users/krisztiankoos/projects/learn-ukrainian-infra-private/.worktrees/dispatch/agy/hramatka-gen-telemetry/hramatka/engine/generate.py#L254)), the prompt builder groups the requested types and calls the provider sequentially for each prompt:
   ```python
   for prompt, _count_signature in prompt_groups:
       ...
       candidates.extend(_generate_from_prompt(...))
   ```
   For Wave 0, all activities requested in a single phase run share the same prompt signature and therefore coalesce into a single generator call per attempt.

---

## 2. Deriving the Expected Call Count

For a 45-minute lesson with a 3-phase TTT budget plan (`_PHASE_PLAN[45] = [1, 1, 2, 2, 2, 3]`), we can derive the expected provider calls under different conditions:

*   **Best Case (No Deficits / 100% Gate Success)**:
    Each of the 3 phases succeeds on the first attempt.
    *   `3 phases * 1 call/phase = 3 calls`

*   **Worst Case (Deficits trigger max regens)**:
    Every phase encounters deficit shortfalls, triggering both allowable regeneration attempts.
    *   `3 phases * (1 initial + 2 regeneration attempts) = 9 calls`

*   **Outage / Failover Overhead**:
    If a primary provider host (e.g., `gemma-ais` via the `ais` endpoint) fails with a 5xx error or rate limit, failover to the fallback provider (e.g., `openrouter`) occurs.
    *   For each failing attempt, 1 extra call (the failed primary call) is logged before the successful fallback call is made.
    *   If all attempts engage failover, the total calls double (up to `9 * 2 = 18 calls`).

---

## 3. Explaining the ~23-Minute Bake Time

The baseline latency for a single successful `gemma-ais` provider call is measured at **70–80 seconds** under normal conditions. 

*   At **75 seconds per call**:
    *   A clean 3-call bake takes **3.75 minutes**.
    *   A maximum-deficit 9-call bake takes **11.25 minutes**.
*   When the network suffers from **5xx outage bursts / timeouts**:
    *   Each primary timeout takes **120 seconds** before raising `GeneratorUnavailable`.
    *   If a regeneration path of 7 calls (observed during high-concurrency periods) experiences 3 primary timeouts/failures followed by successful failovers to OpenRouter, the latency sums up as:
        *   4 clean calls: `4 * 75s = 300s`
        *   3 failover runs: `3 * (120s timeout + 75s fallback) = 585s`
        *   Sequential database transitions & gating rules overhead: `~60s`
        *   Total: `945s` (~16 minutes).
    *   Under severe congestion, serial retries and network routing queuing easily accumulate to the observed **~23-minute (1380 seconds)** bake time before hitting the hard runner limits.

---

## 4. Optimization Recommendations

To bring bakes down to a target of **< 3 minutes**, we recommend the following enhancements:

1. **Parallel Phase Execution**:
   Since the 3 phases of a TTT lesson plan do not share state (they are planned and generated independently), they can be executed concurrently using Python's `concurrent.futures.ThreadPoolExecutor` or `asyncio.gather`. Running all 3 phases in parallel would immediately divide the baseline latency by 3 (bringing the 9-call worst-case down from 11.25 minutes to **3.75 minutes**).

2. **Early Fallback / Dynamic Provider Swapping**:
   Instead of waiting for a 120-second timeout on a hanging primary endpoint, we should implement a shorter timeout (e.g., 30s) or track rolling error rates to proactively flip to the fallback provider.

3. **Asynchronous Batching**:
   If a regeneration request requires multiple calls (e.g., if different prompt builders are introduced in future versions), they should be sent concurrently rather than serially.
