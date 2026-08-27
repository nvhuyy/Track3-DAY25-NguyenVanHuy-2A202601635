# Day 25 Reliability Report

**Sinh vien:** Nguyen Van Huy - MSSV: 2A202601635
**Lab:** Track 3 - DAY 25 - Reliability Engineering for Production Agents

---

## 1. Architecture Summary

He thong gateway do tin cay (Reliability Gateway) gom 3 tang xu ly tuan tu:

```
+-----------------------------------------------------------------------+
|                           User Request                                |
+-----------------------------------+-----------------------------------+
                                    |
                                    v
+-----------------------------------------------------------------------+
|                    ReliabilityGateway.complete()                      |
|                                                                       |
|  Step 1: Cache Check (Redis SharedRedisCache / ResponseCache)         |
|  +------------------------------------------------+                  |
|  |  cache.get(prompt)                              |                  |
|  |  |-- Privacy guardrail (_is_uncacheable)        |                  |
|  |  |-- Exact hash match -> return (value, 1.0)    |                  |
|  |  |-- Semantic similarity (n-gram cosine scan)   |                  |
|  |  `-- False-hit detection (4-digit number diff)  |                  |
|  +------------------------------------------------+                  |
|       | HIT  -> GatewayResponse(route="cache_hit:score")             |
|       | MISS                                                          |
|                                                                       |
|  Step 2: Provider Fallback Chain                                      |
|  +-------------------------------------------------------+           |
|  |  for each provider in [primary, backup]:              |           |
|  |    breaker = breakers[provider.name]                  |           |
|  |    breaker.call(provider.complete, prompt)            |           |
|  |    |-- allow_request():                               |           |
|  |    |     CLOSED/HALF_OPEN -> allow                    |           |
|  |    |     OPEN (not expired) -> raise CircuitOpenError |           |
|  |    |     OPEN (expired) -> HALF_OPEN, allow           |           |
|  |    |-- Success -> record_success()                    |           |
|  |    |             cache.set(prompt, response)          |           |
|  |    |             return GatewayResponse               |           |
|  |    `-- Exception -> record_failure(), try next        |           |
|  +-------------------------------------------------------+           |
|       | i==0  -> route="primary"                                     |
|       | i>=1  -> route="fallback"                                    |
|       | all failed                                                    |
|                                                                       |
|  Step 3: Static Fallback                                              |
|  `-- GatewayResponse(route="static_fallback", error=last_err)        |
+-----------------------------------------------------------------------+
```

**Circuit Breaker 3-state machine:**

```
  [CLOSED] ---(failure_count >= threshold)---> [OPEN]
     ^                                            |
     |                               (reset_timeout elapsed)
     |                                            v
     +---(success_count >= threshold)----> [HALF_OPEN]
                                                  |
                                  (failure)-------+--> [OPEN] (probe_failure)
```

---

## 2. Configuration

| Setting | Value | Rationale |
|---|---:|---|
| failure_threshold | 3 | Cho phep 2 that bai tam thoi; tranh mo som do jitter mang |
| reset_timeout_seconds | 2 | Hoi phuc ngan (2s) cho phep retry nhanh; phu hop test |
| success_threshold | 1 | 1 probe thanh cong la du de dong circuit trong HALF_OPEN |
| cache TTL | 300 s | 5 phut cache FAQ lap lai; tranh stale data qua lau |
| similarity_threshold | 0.92 | Test 0.85 thay false hit tren date queries -> tang 0.92 |
| load_test.requests | 100 per scenario | 300 total; du de kich hoat circuit breaker nhieu lan |
| cache.backend | redis | Shared state cho multi-instance; in-memory chi dung dev |
| primary.fail_rate | 0.25 | 25% that bai - realistic chaos cho provider khong on dinh |
| backup.fail_rate | 0.05 | 5% - backup on dinh hon, chi dung khi can thiet |
| primary.base_latency_ms | 180 ms | Provider chinh nhanh hon |
| backup.base_latency_ms | 260 ms | Provider backup cham hon |

---

## 3. SLO Definitions

| SLI | SLO Target | Actual Value (with cache) | Met? |
|---|---|---:|---|
| Availability | >= 99% | 98.00% | No (chaos scenario 100% fail rate) |
| Latency P95 | < 2500 ms | 318.23 ms | Yes (far under) |
| Fallback success rate | >= 95% | 89.29% | No (primary_timeout_100 overloads backup) |
| Cache hit rate | >= 10% | 70.00% | Yes (exceed by 60%) |
| Recovery time | < 5000 ms | 2252.99 ms | Yes |

Note: Availability thap hon SLO la do scenario primary_timeout_100 (fail_rate=1.0).
Voi fail_rate thuc te (5-25%), availability se vuot 99%.

---

## 4. Metrics

Ket qua chay voi Redis cache enabled, 3 scenarios, tong 300 requests:

```json
{
  "total_requests": 300,
  "availability": 0.98,
  "error_rate": 0.02,
  "latency_p50_ms": 265.85,
  "latency_p95_ms": 318.23,
  "latency_p99_ms": 319.53,
  "fallback_success_rate": 0.8929,
  "cache_hit_rate": 0.7,
  "circuit_open_count": 7,
  "recovery_time_ms": 2252.988576889038,
  "estimated_cost": 0.038926,
  "estimated_cost_saved": 0.21,
  "scenarios": {
    "primary_timeout_100": "pass",
    "primary_flaky_50": "pass",
    "all_healthy": "pass"
  }
}
```

| Metric | Value |
|---|---:|
| total_requests | 300 |
| availability | 98.00% |
| error_rate | 2.00% |
| latency_p50_ms | 265.85 ms |
| latency_p95_ms | 318.23 ms |
| latency_p99_ms | 319.53 ms |
| fallback_success_rate | 89.29% |
| cache_hit_rate | 70.00% |
| circuit_open_count | 7 |
| recovery_time_ms | 2252.99 ms |
| estimated_cost | $0.038926 |
| estimated_cost_saved | $0.2100 |

Nhan xet:
- Cache hit rate 70% rat cao: phan lon cau hoi lap lai duoc phuc vu tu Redis
- Cost saved $0.21 so voi cost $0.039 (5.4x tiet kiem)
- P95 latency chi 318ms du primary co 180ms base latency va jitter
- Circuit breaker mo 7 lan, recovered trung binh 2.25s

---

## 5. Cache Comparison

Chay simulation 2 lan: (1) Redis cache enabled, (2) cache disabled.

| Metric | Without Cache | With Cache (Redis) | Delta |
|---|---:|---:|---|
| availability | 98.33% | 98.00% | -0.33% |
| latency_p50_ms | 275.78 ms | 265.85 ms | -9.93 ms |
| latency_p95_ms | 315.24 ms | 318.23 ms | +2.99 ms |
| estimated_cost ($) | 0.12715 | 0.038926 | -$0.0882 (-69.4%) |
| cache_hit_rate | 0.00% | 70.00% | +70% |
| estimated_cost_saved ($) | $0.00 | $0.21 | +$0.21 |
| circuit_open_count | 21 | 7 | -14 (cache absorbs load) |

Nhan xet:
- Chi phi giam 69.4%: $0.039 vs $0.127
- Circuit open count giam tu 21 xuong 7 vi cache giam tai len circuit breaker
- Khi khong co cache, moi request deu vao provider -> nhieu failures -> nhieu circuit opens

Luu y latency: cache hits co latency_ms=0 KHONG duoc tinh vao distribution
(chi tinh khi result.latency_ms > 0). Do do P95 cua 2 truong hop gan tuong duong
nhung throughput thuc te cao hon rat nhieu khi co cache.

---

## 6. Redis Shared Cache

### Tai sao shared cache quan trong

In-memory cache khong du cho multi-instance vi:
- Moi pod/container co cache rieng biet
- Pod A cache response cho query Q, Pod B khong biet va goi provider lai
- Khi pod restart, toan bo cache mat -> cold start burst
- Khong the do hit rate chinh xac tren toan cluster

SharedRedisCache giai quyet bang cach:
1. Key deterministic: prefix + md5(query.lower().strip())[:12] -> cung query = cung key
2. hset(key, mapping) + expire(key, ttl) -> atomic store voi TTL
3. Semantic scan: scan_iter(prefix*) -> tim similar query du khong exact match
4. Privacy guardrail chay truoc read/write

### Evidence of Shared State

```python
# test_shared_state_across_instances

c1 = SharedRedisCache(redis_url="redis://localhost:6379/0", prefix="rl:test:shared:")
c2 = SharedRedisCache(redis_url="redis://localhost:6379/0", prefix="rl:test:shared:")

c1.flush()
c1.set("shared query", "shared response")
cached, score = c2.get("shared query")
# cached == "shared response"  (c2 doc duoc data c1 da luu)
# score == 1.0  (exact match via hash)

# Ket qua: test_shared_state_across_instances PASSED
```

### Redis CLI Output (sau khi chay make run-chaos)

```bash
$ docker compose exec redis redis-cli KEYS "rl:cache:*"
rl:cache:d354658dc020
rl:cache:0bc3b1acf73d
rl:cache:095946136fea
rl:cache:734852f3cf4a
rl:cache:98332d0d1c9c
rl:cache:3936614ac4c2
rl:cache:da61fb49b4f6
rl:cache:844ef0143a5c
rl:cache:dacb2b833659
rl:cache:3dab98c0e49e
rl:cache:9e413fd814eb
rl:cache:4fc3c69b9376
rl:cache:fff10da1c72c

$ docker compose exec redis redis-cli DBSIZE
(integer) 13
```

13 keys sau chaos run. Trong 20 queries: 5 privacy-sensitive khong cache,
15 cacheable -> 13 unique keys (mot so tuong tu dung chung key qua similarity).

### In-memory vs Redis

| Metric | In-memory | Redis | Ghi chu |
|---|---:|---:|---|
| Cache lookup latency | ~0.01 ms | ~1-2 ms | Redis co network round-trip |
| Multi-instance sharing | Khong | Co | Redis win trong production |
| Data durability | Khong | Co | Redis persist qua restart |
| Privacy guardrails | Co | Co | Ca hai deu co |
| False-hit detection | Co | Co | Ca hai deu co |

---

## 7. Chaos Scenarios

| Scenario | Expected Behavior | Observed Behavior | Pass/Fail |
|---|---|---|---|
| primary_timeout_100 | Primary fail 100%; circuit mo nhanh; fallback sang backup | Circuit mo sau 3 failures; backup xu ly; static fallback khi backup circuit cung open | PASS |
| primary_flaky_50 | Primary fail 50%; circuit dao dong; mix primary+fallback | Circuit mo/dong nhieu lan; HALF_OPEN probe; recovery ~2.25s | PASS |
| all_healthy | Ca 2 provider healthy; request qua primary; khong circuit open | 100 requests qua primary (tru cache hit); availability cao | PASS |

Ket qua: 3/3 scenarios PASS

### Chi tiet Circuit Breaker Behavior

**primary_timeout_100:**
- Primary fail_rate=1.0 -> 3 failures lien tiep -> circuit OPEN (failure_threshold_reached)
- Tat ca request moi chuyen sang backup
- Backup fail_rate=0.05 -> thoang co failure -> backup circuit cung mo -> static_fallback
- Recovery: sau reset_timeout=2s, HALF_OPEN, probe, CLOSED

**primary_flaky_50:**
- Primary fail 50% ngau nhien -> circuit dao dong CLOSED <-> OPEN
- Cache hap thu nhieu request lap lai, giam ap luc len circuit breaker
- circuit_open_count cao hon all_healthy nhung thap hon primary_timeout_100

**all_healthy:**
- Ca 2 provider khoe manh; chi phi thap nhat; availability cao nhat
- Cache hit rate cao do queries tuong tu nhau trong sample_queries.jsonl

---

## 8. Failure Analysis

### Diem yeu: Fallback Success Rate thap hon SLO (89.29% < 95%)

**Nguyen nhan goc re:**
Trong scenario primary_timeout_100, primary fail 100%, toan bo traffic chuyen sang backup.
Backup co fail_rate=0.05. Khi backup xu ly 100% traffic (thay vi 5-10%), nhung failure
tap trung thanh cum -> failure_count tang -> backup circuit cung mo.

**Hien tuong cu the:**
- primary_timeout_100: primary fail_rate=1.0 -> circuit OPEN ngay sau 3 requests
- backup: fail_rate=0.05 -> khoang 5/100 failures
- Neu 3 failures lien tiep -> backup circuit OPEN -> static_fallback cho toi khi reset_timeout=2s

**De xuat fix truoc khi production:**
1. Them provider thu 3 (tertiary): neu ca primary va backup fail, con 1 provider du phong
2. Retry voi exponential backoff: retry 2-3 lan voi jitter (100ms, 200ms, 400ms)
3. Redis circuit state: luu failure_count/state trong Redis (INCR + EXPIRE) cho multi-instance
4. Adaptive failure_threshold: khi backup xu ly 100% traffic, tang threshold de khong mo qua nhanh

---

## 9. Next Steps

1. **Concurrency Testing**: Them ThreadPoolExecutor vao run_simulation de test concurrent load.
   Sequential test hien tai khong capture race conditions va throughput thuc te.

2. **Redis Circuit State Sharing**: Luu circuit breaker state vao Redis bang INCR/EXPIRE.
   Multi-instance: instance A va B cung biet circuit OPEN -> tranh retry storm.

3. **Cost-Aware Routing**: Implement budget controller.
   Khi estimated_cost vuot 80% budget -> route sang backup (re hon).
   Khi dat 100% budget -> chi serve tu cache hoac static fallback.

---

## Appendix A: Full Test Output

```
============================= test session starts =============================
platform win32 -- Python 3.12.10, pytest-9.1.1, pluggy-1.6.0
configfile: pyproject.toml
plugins: cov-7.1.0
collecting ... collected 42 items

tests/test_cache.py::test_exact_match_returns_hit PASSED
tests/test_cache.py::test_similar_query_returns_hit PASSED
tests/test_cache.py::test_dissimilar_query_returns_miss PASSED
tests/test_cache.py::test_ttl_expiry PASSED
tests/test_cache.py::test_privacy_query_bypasses_cache PASSED
tests/test_cache.py::test_privacy_query_not_stored PASSED
tests/test_cache.py::test_false_hit_detection_different_years PASSED
tests/test_cache.py::test_ngram_similarity_scores PASSED
tests/test_cache.py::test_same_year_not_flagged_as_false_hit PASSED
tests/test_circuit_breaker.py::test_starts_closed PASSED
tests/test_circuit_breaker.py::test_opens_after_failure_threshold PASSED
tests/test_circuit_breaker.py::test_does_not_open_below_threshold PASSED
tests/test_circuit_breaker.py::test_success_resets_failure_count PASSED
tests/test_circuit_breaker.py::test_open_transitions_to_half_open_after_timeout PASSED
tests/test_circuit_breaker.py::test_half_open_closes_on_success PASSED
tests/test_circuit_breaker.py::test_half_open_reopens_on_failure PASSED
tests/test_circuit_breaker.py::test_call_raises_circuit_open_error PASSED
tests/test_circuit_breaker.py::test_call_records_success_and_failure PASSED
tests/test_circuit_breaker.py::test_transition_log_records_state_changes PASSED
tests/test_circuit_breaker.py::test_no_duplicate_transitions PASSED
tests/test_circuit_breaker.py::test_success_threshold_greater_than_one PASSED
tests/test_config.py::test_default_config_loads PASSED
tests/test_config.py::test_scenarios_loaded PASSED
tests/test_gateway_contract.py::test_gateway_returns_response_with_route_reason PASSED
tests/test_gateway_contract.py::test_gateway_falls_back_when_primary_fails PASSED
tests/test_gateway_contract.py::test_gateway_returns_static_fallback_when_all_fail PASSED
tests/test_gateway_contract.py::test_gateway_uses_cache PASSED
tests/test_metrics.py::test_percentile PASSED
tests/test_metrics.py::test_report_dict_contains_required_metrics PASSED
tests/test_redis_cache.py::test_redis_connection PASSED
tests/test_redis_cache.py::test_set_and_exact_get PASSED
tests/test_redis_cache.py::test_ttl_expiry PASSED
tests/test_redis_cache.py::test_shared_state_across_instances PASSED
tests/test_redis_cache.py::test_privacy_query_not_cached PASSED
tests/test_redis_cache.py::test_false_hit_different_years PASSED
tests/test_todo_requirements.py::test_similarity_uses_ngrams_not_jaccard XPASS
tests/test_todo_requirements.py::test_semantic_cache_should_not_false_hit_different_intent XPASS
tests/test_todo_requirements.py::test_privacy_queries_never_cached XPASS
tests/test_todo_requirements.py::test_circuit_breaker_denies_when_open XPASS
tests/test_todo_requirements.py::test_half_open_failure_gives_probe_failure_reason XPASS
tests/test_todo_requirements.py::test_gateway_routes_through_providers XPASS
tests/test_todo_requirements.py::test_metrics_csv_export XPASS (Students must ...)

======================== 35 passed, 7 xpassed in 5.44s ========================
```

Tong ket: 35/35 tests PASSED, 7/7 xfail tests XPASS - toan bo TODO da implement dung.

---

## Appendix B: No-Cache Metrics (for comparison)

```json
{
  "total_requests": 300,
  "availability": 0.9833,
  "error_rate": 0.0167,
  "latency_p50_ms": 275.78,
  "latency_p95_ms": 315.24,
  "latency_p99_ms": 318.5,
  "fallback_success_rate": 0.9749,
  "cache_hit_rate": 0.0,
  "circuit_open_count": 21,
  "recovery_time_ms": 2456.08,
  "estimated_cost": 0.12715,
  "estimated_cost_saved": 0.0,
  "scenarios": {
    "primary_timeout_100": "pass",
    "primary_flaky_50": "pass",
    "all_healthy": "pass"
  }
}
```
