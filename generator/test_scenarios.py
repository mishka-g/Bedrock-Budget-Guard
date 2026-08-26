import engine

SCHEMA_INPUT_KEYS = {"inputTokenCount", "cacheReadInputTokenCount", "cacheWriteInputTokenCount"}


def _alpha_count(cfg, minute):
    return sum(
        1 for _, rec in engine.events_for_minute(cfg, minute)
        if "proj-alpha-app" in rec["identity"]["arn"]
    )


def test_same_seed_same_events():
    cfg_a = engine.load_config("scenarios.yaml")
    cfg_b = engine.load_config("scenarios.yaml")
    for minute in (0, 7, 12, 30):
        assert engine.events_for_minute(cfg_a, minute) == engine.events_for_minute(cfg_b, minute)
    assert len(engine.events_for_minute(cfg_a, 0)) > 0


def _burst_config():
    # Synthetic config exercising the timeline machinery (the shipped
    # scenarios.yaml currently has no timeline events).
    dist = engine.TokenDist(mean=100, spread=10)
    traffic = engine.Traffic(
        role="proj-alpha-app",
        model="eu.anthropic.claude-haiku-4-5-20251001-v1:0",
        calls_per_minute=6,
        input_tokens=dist, output_tokens=dist,
        cache_read_tokens=dist, cache_write_tokens=dist,
    )
    burst = engine.TimelineEvent(
        at_minute=10, duration_minutes=15,
        role="proj-alpha-app", rate_multiplier=8,
    )
    return engine.Config(seed=7, baseline=(traffic,), timeline=(burst,))


def test_burst_multiplies_alpha_rate():
    cfg = _burst_config()
    baseline = sum(_alpha_count(cfg, m) for m in range(0, 5))
    burst = sum(_alpha_count(cfg, m) for m in range(10, 15))
    assert burst > baseline * 4  # x8 nominal, leave slack for rounding


def test_burst_ends_after_duration():
    cfg = _burst_config()
    after = sum(_alpha_count(cfg, m) for m in range(25, 30))
    burst = sum(_alpha_count(cfg, m) for m in range(10, 15))
    assert after < burst / 4


def test_record_shape_matches_bedrock_logs():
    cfg = engine.load_config("scenarios.yaml")
    offset, rec = engine.events_for_minute(cfg, 0)[0]
    assert 0 <= offset < 60
    assert rec["region"] == "eu-west-1"
    assert rec["modelId"].startswith("eu.anthropic.")
    assert rec["identity"]["arn"].startswith("arn:aws:sts::000000000000:assumed-role/")
    assert set(rec["input"].keys()) == SCHEMA_INPUT_KEYS
    assert set(rec["output"].keys()) == {"outputTokenCount"}
    assert all(v >= 0 for v in rec["input"].values())
    assert "timestamp" not in rec  # the delivery loop stamps it


def test_offsets_sorted():
    cfg = engine.load_config("scenarios.yaml")
    offsets = [o for o, _ in engine.events_for_minute(cfg, 3)]
    assert offsets == sorted(offsets)
