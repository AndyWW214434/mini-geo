"""Where answers come from.

Bright Data's AI scrapers are asynchronous: trigger a snapshot with N prompts, poll until it
is ready, download it. `AnswerSource` captures exactly that contract so the workflow does
not care whether answers come from Bright Data or from the synthetic generator.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol

import httpx

from geo.catalog import Prompt
from geo.config import settings


class SourceError(Exception):
    """Permanent failure (bad request, auth). Not retried."""


class RateLimited(Exception):
    """429 or 5xx from the provider. Retried by Temporal with backoff."""


@dataclass(frozen=True)
class SnapshotStatus:
    state: str  # running | ready | failed
    detail: str | None = None


class AnswerSource(Protocol):
    name: str

    def trigger(self, platform_id: str, dataset_id: str, inputs: list[dict[str, Any]], window: str) -> str: ...
    def status(self, snapshot_id: str) -> SnapshotStatus: ...
    def download(self, snapshot_id: str) -> str: ...
    def records(self, snapshot_id: str) -> SnapshotRecords: ...


@dataclass
class SnapshotRecords:
    records: list[dict[str, Any]]   # slimmed: page dumps replaced by size + hash
    original_bytes: int
    original_sha256: str


class BrightDataSource:
    name = "brightdata"
    base = "https://api.brightdata.com/datasets/v3"

    def __init__(self, token: str, timeout: float = 60.0):
        self._http = httpx.Client(timeout=timeout, headers={"Authorization": f"Bearer {token}"})

    def _check(self, resp: httpx.Response) -> httpx.Response:
        if resp.status_code == 429 or resp.status_code >= 500:
            raise RateLimited(f"{resp.status_code}: {resp.text[:300]}")
        if resp.status_code >= 400:
            raise SourceError(f"{resp.status_code}: {resp.text[:300]}")
        return resp

    def trigger(self, platform_id: str, dataset_id: str, inputs: list[dict[str, Any]], window: str) -> str:
        resp = self._check(self._http.post(f"{self.base}/trigger",
                                           params={"dataset_id": dataset_id, "include_errors": "true",
                                                   "format": "json"}, json=inputs))
        return resp.json()["snapshot_id"]

    def status(self, snapshot_id: str) -> SnapshotStatus:
        body = self._check(self._http.get(f"{self.base}/progress/{snapshot_id}")).json()
        state = body.get("status", "running")
        if state == "ready":
            return SnapshotStatus("ready")
        if state in ("failed", "canceled"):
            return SnapshotStatus("failed", json.dumps(body)[:500])
        return SnapshotStatus("running")

    def records(self, snapshot_id: str) -> SnapshotRecords:
        """Stream the snapshot one record per line and slim each as it arrives.

        A 20-answer snapshot is ~80 MB of page dumps; loading it whole ran a 512 MB worker out of
        memory. Memory now holds one record at a time.
        """
        from geo.etl.pipeline import slim_record

        digest, size, out = hashlib.sha256(), 0, []
        with self._http.stream("GET", f"{self.base}/snapshot/{snapshot_id}", params={"format": "jsonl"},
                               timeout=httpx.Timeout(60, read=600)) as resp:
            if resp.status_code == 202:
                raise RateLimited("snapshot not ready for download")
            if resp.status_code >= 400:
                resp.read()
                self._check(resp)
            for line in resp.iter_lines():
                data = line.encode()
                digest.update(data + b"\n")
                size += len(data) + 1
                if line.strip():
                    rec = json.loads(line)
                    if isinstance(rec, dict):
                        out.append(slim_record(rec))
        return SnapshotRecords(out, size, digest.hexdigest())

    def download(self, snapshot_id: str) -> str:
        resp = self._check(self._http.get(f"{self.base}/snapshot/{snapshot_id}", params={"format": "json"}))
        if resp.status_code == 202:  # still building the file
            raise RateLimited("snapshot not ready for download")
        return resp.text


class SyntheticSource:
    """Generates Bright Data-shaped records locally. Used when no Bright Data token is set."""

    name = "synthetic"

    def __init__(self, epoch: date):
        from geo.catalog import load_brands, load_prompts
        from geo.synthetic.generator import Generator

        self._prompts = {p.text: p for p in load_prompts()}
        self._gen = Generator(load_brands(), list(self._prompts.values()), epoch)

    def trigger(self, platform_id: str, dataset_id: str, inputs: list[dict[str, Any]], window: str) -> str:
        return "synthetic:" + json.dumps({"platform": platform_id, "window": window,
                                          "prompts": [i["prompt"] for i in inputs]})

    def status(self, snapshot_id: str) -> SnapshotStatus:
        return SnapshotStatus("ready")

    def records(self, snapshot_id: str) -> SnapshotRecords:
        body = self.download(snapshot_id)
        return SnapshotRecords(json.loads(body), len(body.encode()), hashlib.sha256(body.encode()).hexdigest())

    def download(self, snapshot_id: str) -> str:
        spec = json.loads(snapshot_id.removeprefix("synthetic:"))
        window = datetime.fromisoformat(spec["window"])
        records = []
        for text in spec["prompts"]:
            prompt = self._prompts.get(text) or Prompt("adhoc", "payments", text)
            records.append(self._gen.record(spec["platform"], prompt, window))
        return json.dumps(records)


def source_for(name: str | None = None) -> AnswerSource:
    s = settings()
    name = name or s.effective_source
    if name == "brightdata":
        if not s.brightdata_api_token:
            raise SourceError("BRIGHTDATA_API_TOKEN is not set")
        return BrightDataSource(s.brightdata_api_token)
    return SyntheticSource(s.synthetic_epoch)
