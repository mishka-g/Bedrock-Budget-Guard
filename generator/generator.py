"""bedrock-simulator: streams scenario-driven Bedrock invocation-log
records into ministack CloudWatch Logs.

Delivery rules:
  - Only fully elapsed minutes are emitted, so every event timestamp is
    in the past (mirrors how real invocation logs arrive with a lag).
  - PutLogEvents is retried with exponential backoff; the log stream is
    recreated if ministack lost state (restart), so the generator
    survives emulator restarts without exiting.
  - A heartbeat line every 60s reports cumulative counters, so
    "is data flowing" is answerable from docker compose logs.
"""
import datetime
import json
import logging
import os
import time
from collections import Counter

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

import denycheck
import engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bedrock-simulator")

LOG_GROUP = "/aws/bedrock/modelinvocations"
STREAM = "bedrock-simulator"
MAX_ATTEMPTS = 6


def _client():
    return boto3.client(
        "logs",
        endpoint_url=os.environ["AWS_ENDPOINT_URL"],
        config=BotoConfig(
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=5,
            read_timeout=15,
        ),
    )


def _iam_client():
    return boto3.client(
        "iam",
        endpoint_url=os.environ["AWS_ENDPOINT_URL"],
        config=BotoConfig(
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=5,
            read_timeout=15,
        ),
    )


def _ensure_stream(client) -> None:
    try:
        client.create_log_stream(logGroupName=LOG_GROUP, logStreamName=STREAM)
        log.info("created log stream %s in %s", STREAM, LOG_GROUP)
    except client.exceptions.ResourceAlreadyExistsException:
        pass


def _iso(epoch: float) -> str:
    return datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _put_with_retry(client, batch: list[dict]) -> None:
    """batch: CloudWatch-ready [{timestamp: ms, message: str}] sorted by ts."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            client.put_log_events(
                logGroupName=LOG_GROUP, logStreamName=STREAM, logEvents=batch,
            )
            return
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ResourceNotFoundException":
                # ministack restarted and lost state; recreate and retry.
                try:
                    client.create_log_group(logGroupName=LOG_GROUP)
                except client.exceptions.ResourceAlreadyExistsException:
                    pass
                _ensure_stream(client)
                if attempt == MAX_ATTEMPTS:
                    raise
            elif attempt == MAX_ATTEMPTS:
                raise
        except BotoCoreError:
            if attempt == MAX_ATTEMPTS:
                raise
        sleep_s = min(2 ** attempt, 30)
        log.warning("put_log_events attempt %d failed; retrying in %ss", attempt, sleep_s)
        time.sleep(sleep_s)


def main() -> None:
    config = engine.load_config(os.environ.get("SCENARIOS_PATH", "scenarios.yaml"))
    client = _client()
    iam = _iam_client()

    # Startup: wait for ministack (compose ordering makes this rare).
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            _ensure_stream(client)
            break
        except (ClientError, BotoCoreError) as exc:
            if attempt == MAX_ATTEMPTS:
                raise
            log.warning("startup attempt %d failed (%s); retrying", attempt, exc)
            time.sleep(min(2 ** attempt, 30))

    start = time.time()
    emitted_through = -1  # last minute index already written
    written = Counter()
    last_heartbeat = 0.0
    log.info("streaming with seed=%d, %d baseline entries, %d timeline events",
             config.seed, len(config.baseline), len(config.timeline))

    while True:
        current_minute = int((time.time() - start) // 60)
        # Emit only COMPLETED minutes (strictly before the current one),
        # so timestamps are always in the past.
        while emitted_through < current_minute - 1:
            emitted_through += 1
            events = engine.events_for_minute(config, emitted_through)
            if events:
                by_role = {}
                for offset, record in events:
                    role = record["identity"]["arn"].split("/")[1]
                    by_role.setdefault(role, []).append((offset, record))
                kept = []
                for role in sorted(by_role):
                    if denycheck.role_is_blocked(iam, role):
                        log.info(
                            "role %s is blocked in IAM, suppressed %d events",
                            role, len(by_role[role]),
                        )
                        continue
                    kept.extend(by_role[role])
                kept.sort(key=lambda pair: pair[0])
                if kept:
                    batch = []
                    for offset, record in kept:
                        epoch = start + emitted_through * 60 + offset
                        message = json.dumps({"timestamp": _iso(epoch), **record})
                        batch.append({"timestamp": int(epoch * 1000), "message": message})
                    _put_with_retry(client, batch)
                    for _, record in kept:
                        role = record["identity"]["arn"].split("/")[1]
                        written[role] += 1
        if time.time() - last_heartbeat >= 60:
            last_heartbeat = time.time()
            log.info(
                "heartbeat: minute=%d written_total=%d per_role=%s",
                current_minute, sum(written.values()), dict(written),
            )
        time.sleep(5)


if __name__ == "__main__":
    main()
