"""Shared structured observability helpers for long-running commands."""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
import time
from collections.abc import Iterator

import click


def obs_s(value: object) -> str:
    return str(value).replace("\n", " ").replace("\r", " ").replace(" ", "_")


def env_interval_seconds(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    with contextlib.suppress(ValueError):
        return max(int(raw), 0)
    return default


class StructuredObs:
    """Emit single-line key=value observability events."""

    def __init__(self, prefix: str, *, err: bool = False) -> None:
        self.prefix = prefix
        self.err = err
        self.run_start = time.monotonic()

    def duration(self) -> float:
        return round(time.monotonic() - self.run_start, 3)

    def emit(self, event: str, **fields: object) -> None:
        parts = [f"event={obs_s(event)}"]
        for key, value in fields.items():
            parts.append(f"{key}={obs_s(value)}")
        click.echo(f"{self.prefix} " + " ".join(parts), err=self.err)


async def async_heartbeat_loop(
    obs: StructuredObs,
    *,
    event: str,
    start: float,
    stop_event: asyncio.Event,
    interval_s: int,
    fields: dict[str, object],
) -> None:
    if interval_s <= 0:
        return
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
            return
        except TimeoutError:
            obs.emit(
                event,
                **fields,
                elapsed_s=round(time.monotonic() - start, 3),
                interval_s=interval_s,
                note="still_processing",
            )


@contextlib.contextmanager
def sync_heartbeat(
    obs: StructuredObs,
    *,
    event: str,
    start: float,
    interval_s: int,
    fields: dict[str, object],
) -> Iterator[None]:
    stop = threading.Event()

    def _run() -> None:
        if interval_s <= 0:
            return
        while not stop.wait(interval_s):
            obs.emit(
                event,
                **fields,
                elapsed_s=round(time.monotonic() - start, 3),
                interval_s=interval_s,
                note="still_processing",
            )

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1)
