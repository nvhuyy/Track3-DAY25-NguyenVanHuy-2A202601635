from __future__ import annotations

import json
import random
from pathlib import Path

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.config import LabConfig, ScenarioConfig
from reliability_lab.gateway import ReliabilityGateway
from reliability_lab.metrics import RunMetrics
from reliability_lab.providers import FakeLLMProvider


def load_queries(path: str | Path = "data/sample_queries.jsonl") -> list[str]:
    queries: list[str] = []

    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue

        queries.append(json.loads(line)["query"])

    return queries


def build_gateway(
    config: LabConfig,
    provider_overrides: dict[str, float] | None = None,
) -> ReliabilityGateway:
    providers = []

    for p in config.providers:
        fail_rate = (
            provider_overrides.get(p.name, p.fail_rate)
            if provider_overrides
            else p.fail_rate
        )

        providers.append(
            FakeLLMProvider(
                p.name,
                fail_rate,
                p.base_latency_ms,
                p.cost_per_1k_tokens,
            )
        )

    breakers = {
        p.name: CircuitBreaker(
            name=p.name,
            failure_threshold=config.circuit_breaker.failure_threshold,
            reset_timeout_seconds=config.circuit_breaker.reset_timeout_seconds,
            success_threshold=config.circuit_breaker.success_threshold,
        )
        for p in config.providers
    }

    cache: ResponseCache | SharedRedisCache | None = None

    if config.cache.enabled:
        if config.cache.backend == "redis":
            cache = SharedRedisCache(
                config.cache.redis_url,
                config.cache.ttl_seconds,
                config.cache.similarity_threshold,
            )
        else:
            cache = ResponseCache(
                config.cache.ttl_seconds,
                config.cache.similarity_threshold,
            )

    return ReliabilityGateway(
        providers,
        breakers,
        cache,
    )


def calculate_recovery_time_ms(
    gateway: ReliabilityGateway,
) -> float | None:
    """Derive recovery time from circuit breaker transition logs.

    Recovery time is measured from an OPEN transition to the subsequent
    CLOSED transition for each circuit breaker.
    """

    recovery_times_ms: list[float] = []

    for breaker in gateway.breakers.values():
        opened_at: float | None = None

        for transition in breaker.transition_log:
            transition_to = transition["to"]
            timestamp = float(transition["ts"])

            if transition_to == "open":
                # Record the latest time the circuit entered OPEN.
                opened_at = timestamp

            elif transition_to == "closed" and opened_at is not None:
                recovery_time_ms = (
                    timestamp - opened_at
                ) * 1000.0

                recovery_times_ms.append(recovery_time_ms)

                # This OPEN -> CLOSED pair has been consumed.
                opened_at = None

    if not recovery_times_ms:
        return None

    return sum(recovery_times_ms) / len(recovery_times_ms)


def run_scenario(
    config: LabConfig,
    queries: list[str],
    scenario: ScenarioConfig,
) -> RunMetrics:
    """Run a single named chaos scenario."""

    # 1. Build gateway with scenario-specific provider overrides.
    gateway = build_gateway(
        config,
        scenario.provider_overrides or None,
    )

    # 2. Create empty metrics.
    metrics = RunMetrics()

    # 3. Execute load-test requests.
    for _ in range(config.load_test.requests):
        prompt = random.choice(queries)

        result = gateway.complete(prompt)

        # Every call counts as a request.
        metrics.total_requests += 1

        # Accumulate provider/cache cost.
        metrics.estimated_cost += result.estimated_cost

        # --------------------------------------------------------------
        # Cache hit
        # --------------------------------------------------------------
        # A cache hit is a successful request because the user received
        # a valid response.
        if result.cache_hit:
            metrics.cache_hits += 1
            metrics.estimated_cost_saved += 0.001
            metrics.successful_requests += 1

        # --------------------------------------------------------------
        # Fallback provider
        # --------------------------------------------------------------
        elif result.route == "fallback":
            metrics.fallback_successes += 1
            metrics.successful_requests += 1

        # --------------------------------------------------------------
        # Static fallback
        # --------------------------------------------------------------
        elif result.route == "static_fallback":
            metrics.static_fallbacks += 1
            metrics.failed_requests += 1

        # --------------------------------------------------------------
        # Primary provider
        # --------------------------------------------------------------
        else:
            metrics.successful_requests += 1

        # Only record actual measured latency.
        if result.latency_ms > 0:
            metrics.latencies_ms.append(result.latency_ms)

    # 4. Count every transition into OPEN across all circuit breakers.
    metrics.circuit_open_count = sum(
        1
        for breaker in gateway.breakers.values()
        for transition in breaker.transition_log
        if transition["to"] == "open"
    )

    # 5. Calculate average recovery time.
    metrics.recovery_time_ms = calculate_recovery_time_ms(
        gateway
    )

    # 6. Return scenario metrics.
    return metrics


def run_simulation(
    config: LabConfig,
    queries: list[str],
) -> RunMetrics:
    """Run all named scenarios from config, or a default run if none defined.

    Also supports aggregating results from multiple scenarios into one
    RunMetrics object.
    """

    # No scenarios configured -> run a default scenario.
    if not config.scenarios:
        default_scenario = ScenarioConfig(
            name="default",
            description="baseline run",
        )

        metrics = run_scenario(
            config,
            queries,
            default_scenario,
        )

        metrics.scenarios = {
            "default": (
                "pass"
                if metrics.successful_requests > 0
                else "fail"
            )
        }

        return metrics

    # Aggregate results from all configured scenarios.
    combined = RunMetrics()

    for scenario in config.scenarios:
        result = run_scenario(
            config,
            queries,
            scenario,
        )

        # --------------------------------------------------------------
        # Scenario pass/fail criterion
        # --------------------------------------------------------------
        # A scenario passes if at least one request succeeds.
        passed = result.successful_requests > 0

        combined.scenarios[scenario.name] = (
            "pass" if passed else "fail"
        )

        # --------------------------------------------------------------
        # Aggregate metrics
        # --------------------------------------------------------------
        combined.total_requests += result.total_requests

        combined.successful_requests += (
            result.successful_requests
        )

        combined.failed_requests += result.failed_requests

        combined.fallback_successes += (
            result.fallback_successes
        )

        combined.static_fallbacks += (
            result.static_fallbacks
        )

        combined.cache_hits += result.cache_hits

        combined.circuit_open_count += (
            result.circuit_open_count
        )

        combined.estimated_cost += (
            result.estimated_cost
        )

        combined.estimated_cost_saved += (
            result.estimated_cost_saved
        )

        combined.latencies_ms.extend(
            result.latencies_ms
        )

        # --------------------------------------------------------------
        # Aggregate recovery time
        # --------------------------------------------------------------
        if result.recovery_time_ms is not None:
            if combined.recovery_time_ms is None:
                combined.recovery_time_ms = (
                    result.recovery_time_ms
                )
            else:
                combined.recovery_time_ms = (
                    combined.recovery_time_ms
                    + result.recovery_time_ms
                ) / 2

    return combined
