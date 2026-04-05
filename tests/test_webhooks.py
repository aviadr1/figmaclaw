"""Tests for commands/webhooks.py — Figma file-webhook management.

INVARIANT (system under test):
    For every tracked file, there is EXACTLY ONE active webhook pointing to
    the configured endpoint.

The validate / sync / register coroutines enforce this invariant. Tests stub
the FigmaClient file-webhook methods with in-memory fakes — we exercise the
decision logic, not HTTP.
"""
from __future__ import annotations

import asyncio
import os
from contextlib import ExitStack
from unittest.mock import patch

import pytest

from figmaclaw.commands import webhooks as sut
from figmaclaw.figma_models import ValidationReport, Webhook


# ---------------------------------------------------------------------------
# Constants & helpers
# ---------------------------------------------------------------------------

ENDPOINT = "https://proxy.example.com/figma"
OTHER_ENDPOINT = "https://old-proxy.example.com/figma"


def _wh(file_key: str, endpoint: str = ENDPOINT, *, wh_id: str | None = None) -> Webhook:
    """Build a Webhook model instance. Uses id(object()) for unique default IDs."""
    if wh_id is None:
        wh_id = f"wh-{id(object())}"
    return Webhook(id=wh_id, context_id=file_key, endpoint=endpoint)


class FakeFigmaClient:
    """In-memory stand-in for FigmaClient — records creates/deletes for assertions.

    Implements the three file-webhook methods used by commands/webhooks.py:
      * list_file_webhooks
      * create_file_webhook
      * delete_webhook
    """

    def __init__(self, webhooks_by_file: dict[str, list[Webhook]] | None = None):
        self.webhooks_by_file: dict[str, list[Webhook]] = {
            k: list(v) for k, v in (webhooks_by_file or {}).items()
        }
        self.created: list[Webhook] = []
        self.deleted: list[str] = []
        self._create_counter = 0

    async def list_file_webhooks(self, file_key: str):
        return [wh.model_dump() for wh in self.webhooks_by_file.get(file_key, [])]

    async def create_file_webhook(self, file_key: str, endpoint: str, passcode: str, **_):
        self._create_counter += 1
        wh = _wh(file_key, endpoint, wh_id=f"new-{self._create_counter}")
        self.created.append(wh)
        self.webhooks_by_file.setdefault(file_key, []).append(wh)
        return wh.model_dump()

    async def delete_webhook(self, wh_id: str) -> None:
        self.deleted.append(wh_id)
        for whs in self.webhooks_by_file.values():
            whs[:] = [w for w in whs if w.id != wh_id]


@pytest.fixture(autouse=True)
def _stub_webhook_secret():
    """Ensure _require_passcode finds a value (so tests don't print warnings)."""
    with patch.dict(os.environ, {"FIGMA_WEBHOOK_SECRET": "test-secret"}):
        yield


def _run(coro):
    return asyncio.run(coro)


# ===================================================================
# SUT: validate — read-only invariant checker
# ===================================================================

class TestValidateInvariant:
    """validate() must detect every class of invariant violation."""

    def test_passes_when_invariant_holds(self):
        """INVARIANT: exactly one webhook per file -> validate returns ok."""
        files = ["file-A", "file-B"]
        client = FakeFigmaClient({"file-A": [_wh("file-A")], "file-B": [_wh("file-B")]})
        report = _run(sut.validate(client, ENDPOINT, files))
        assert report.ok

    def test_detects_missing_webhook(self):
        """INVARIANT VIOLATION: a tracked file has zero webhooks."""
        files = ["file-A", "file-B"]
        client = FakeFigmaClient({"file-A": [_wh("file-A")]})
        report = _run(sut.validate(client, ENDPOINT, files))
        assert not report.ok
        assert report.missing == ["file-B"]

    def test_detects_duplicate_webhooks(self):
        """INVARIANT VIOLATION: a tracked file has >1 webhook for same endpoint."""
        files = ["file-A"]
        client = FakeFigmaClient({"file-A": [_wh("file-A"), _wh("file-A")]})
        report = _run(sut.validate(client, ENDPOINT, files))
        assert not report.ok
        assert len(report.duplicates) == 1
        assert report.duplicates[0][0] == "file-A"

    def test_detects_stale_webhook_wrong_endpoint(self):
        """INVARIANT VIOLATION: webhook exists but points to wrong endpoint."""
        files = ["file-A"]
        client = FakeFigmaClient({
            "file-A": [_wh("file-A"), _wh("file-A", OTHER_ENDPOINT)],
        })
        report = _run(sut.validate(client, ENDPOINT, files))
        assert not report.ok
        assert len(report.stale) == 1
        assert report.stale[0].endpoint == OTHER_ENDPOINT

    def test_missing_and_duplicate_detected_together(self):
        """validate must report ALL violations, not short-circuit on the first."""
        files = ["file-A", "file-B"]
        client = FakeFigmaClient({"file-A": [_wh("file-A"), _wh("file-A")]})
        report = _run(sut.validate(client, ENDPOINT, files))
        assert not report.ok
        assert report.missing == ["file-B"]
        assert len(report.duplicates) == 1


# ===================================================================
# SUT: sync — enforce invariant (create missing, delete duplicates)
# ===================================================================

class TestSyncEnforcesInvariant:
    """sync() must bring the system to the invariant state."""

    def test_creates_missing_webhooks(self):
        """sync must create a webhook for every tracked file that has none."""
        client = FakeFigmaClient()
        _run(sut.sync(client, ENDPOINT, ["file-A", "file-B"]))
        assert len(client.created) == 2
        assert {wh.context_id for wh in client.created} == {"file-A", "file-B"}

    def test_removes_duplicate_webhooks(self):
        """sync must delete extras, keeping exactly one per file."""
        wh1 = _wh("file-A", wh_id="keep")
        wh2 = _wh("file-A", wh_id="dup-1")
        wh3 = _wh("file-A", wh_id="dup-2")
        client = FakeFigmaClient({"file-A": [wh1, wh2, wh3]})
        _run(sut.sync(client, ENDPOINT, ["file-A"]))
        assert set(client.deleted) == {"dup-1", "dup-2"}

    def test_idempotent_when_invariant_already_holds(self):
        """INVARIANT: sync on a clean state makes zero API mutations."""
        files = ["file-A", "file-B"]
        client = FakeFigmaClient({"file-A": [_wh("file-A")], "file-B": [_wh("file-B")]})
        _run(sut.sync(client, ENDPOINT, files))
        assert client.created == []
        assert client.deleted == []

    def test_creates_and_deduplicates_in_one_pass(self):
        """sync handles mixed state: some files missing, others duplicated."""
        wh1 = _wh("file-B", wh_id="keep")
        wh2 = _wh("file-B", wh_id="dup")
        client = FakeFigmaClient({"file-B": [wh1, wh2]})
        _run(sut.sync(client, ENDPOINT, ["file-A", "file-B"]))
        assert len(client.created) == 1
        assert client.created[0].context_id == "file-A"
        assert client.deleted == ["dup"]

    def test_dry_run_makes_no_mutations(self):
        """dry_run=True must fetch state but never create or delete."""
        wh1 = _wh("file-B", wh_id="keep")
        wh2 = _wh("file-B", wh_id="dup")
        client = FakeFigmaClient({"file-B": [wh1, wh2]})
        _run(sut.sync(client, ENDPOINT, ["file-A", "file-B"], dry_run=True))
        assert client.created == []
        assert client.deleted == []


# ===================================================================
# SUT: register — conservative add-only
# ===================================================================

class TestRegisterAddOnly:
    """register() must only add, never delete — even when duplicates exist."""

    def test_creates_missing_webhooks(self):
        """register creates webhooks for files that have none."""
        client = FakeFigmaClient({"file-A": [_wh("file-A")]})
        _run(sut.register(client, ENDPOINT, ["file-A", "file-B"]))
        assert len(client.created) == 1
        assert client.created[0].context_id == "file-B"

    def test_never_deletes_duplicates(self):
        """INVARIANT: register must never delete, even when duplicates exist."""
        client = FakeFigmaClient({"file-A": [_wh("file-A"), _wh("file-A")]})
        _run(sut.register(client, ENDPOINT, ["file-A"]))
        assert client.deleted == [], "register must never delete webhooks"

    def test_skips_files_that_already_have_webhook(self):
        """register must not create a second webhook for a covered file."""
        client = FakeFigmaClient({"file-A": [_wh("file-A")]})
        _run(sut.register(client, ENDPOINT, ["file-A"]))
        assert client.created == []


# ===================================================================
# SUT: _group_webhooks_by_file — shared helper
# ===================================================================

class TestGroupWebhooksByFile:
    def test_groups_by_file_key_filtering_endpoint(self):
        webhooks = [
            _wh("file-A", ENDPOINT),
            _wh("file-A", OTHER_ENDPOINT),
            _wh("file-B", ENDPOINT),
        ]
        grouped = sut._group_webhooks_by_file(webhooks, ENDPOINT)
        assert set(grouped.keys()) == {"file-A", "file-B"}
        assert len(grouped["file-A"]) == 1
        assert len(grouped["file-B"]) == 1

    def test_returns_empty_for_no_matches(self):
        webhooks = [_wh("file-A", OTHER_ENDPOINT)]
        grouped = sut._group_webhooks_by_file(webhooks, ENDPOINT)
        assert dict(grouped) == {}


# ===================================================================
# SUT: ValidationReport model
# ===================================================================

class TestValidationReport:
    def test_ok_when_empty(self):
        assert ValidationReport().ok

    def test_not_ok_with_missing(self):
        assert not ValidationReport(missing=["file-A"]).ok

    def test_not_ok_with_duplicates(self):
        wh = _wh("file-A")
        assert not ValidationReport(duplicates=[("file-A", [wh, wh])]).ok

    def test_not_ok_with_stale(self):
        assert not ValidationReport(stale=[_wh("file-A", OTHER_ENDPOINT)]).ok


# ===================================================================
# SUT: delete_all — bulk cleanup
# ===================================================================

class TestDeleteAll:
    def test_deletes_everything_when_no_filter(self):
        client = FakeFigmaClient({
            "file-A": [_wh("file-A"), _wh("file-A", OTHER_ENDPOINT)],
            "file-B": [_wh("file-B")],
        })
        _run(sut.delete_all(client, ["file-A", "file-B"]))
        assert len(client.deleted) == 3

    def test_endpoint_filter_deletes_only_matching(self):
        client = FakeFigmaClient({
            "file-A": [_wh("file-A", wh_id="keep")],
            "file-B": [_wh("file-B", OTHER_ENDPOINT, wh_id="drop")],
        })
        _run(sut.delete_all(client, ["file-A", "file-B"], endpoint_filter=OTHER_ENDPOINT))
        assert client.deleted == ["drop"]
