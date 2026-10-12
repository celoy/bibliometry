"""Append-only, de-duplicated, gzip-compressed JSONL store of raw OpenAlex records.

Full OpenAlex work records weigh ~30 kB each (references, locations, topics…), so a
five-year institutional corpus runs to a few hundred MB uncompressed; gzip brings that
down roughly tenfold. Each append opens a new gzip member, which Python reads back
transparently as one stream.

Records are written verbatim: rebuilding tables never needs another API call, and any
field we later decide we need is already on disk.
"""

from __future__ import annotations

import gzip
import json
import warnings
import zlib
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .openalex import short_id


class WorksStore:
    """Raw-record store keyed on the OpenAlex id.

    Args:
        path: a ``.jsonl.gz`` file; created on first write.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ids: set[str] = set()
        if self.path.exists():
            self._load_ids()

    def _read_lines(self) -> Iterator[str]:
        with gzip.open(self.path, "rt", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    yield line

    def _load_ids(self) -> None:
        good: list[str] = []
        try:
            for line in self._read_lines():
                record = json.loads(line)
                good.append(line)
                work_id = short_id(record.get("id"))
                if work_id:
                    self._ids.add(work_id)
        except (EOFError, gzip.BadGzipFile, zlib.error, json.JSONDecodeError):
            # A run killed mid-write leaves a truncated last member. Keep every record
            # that reads back cleanly and drop the tail; the next harvest re-fetches it.
            warnings.warn(
                f"{self.path} ended mid-record (interrupted run); recovered "
                f"{len(good)} records and dropped the damaged tail.",
                stacklevel=2,
            )
            self._rewrite(good)

    def _rewrite(self, lines: list[str]) -> None:
        temporary = self.path.with_suffix(".tmp")
        with gzip.open(temporary, "wt", encoding="utf-8") as handle:
            for line in lines:
                handle.write(line + "\n")
        temporary.replace(self.path)

    def __contains__(self, record_id: str) -> bool:
        return record_id in self._ids

    def __len__(self) -> int:
        return len(self._ids)

    @property
    def ids(self) -> set[str]:
        return set(self._ids)

    def add_many(self, records: Iterable[dict[str, Any]]) -> int:
        """Append records whose id is not already stored. Returns how many were written."""
        fresh = []
        for record in records:
            record_id = short_id(record.get("id"))
            if record_id and record_id not in self._ids:
                fresh.append(record)
                self._ids.add(record_id)
        if fresh:
            with gzip.open(self.path, "at", encoding="utf-8") as handle:
                for record in fresh:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return len(fresh)

    def iter_records(self) -> Iterator[dict[str, Any]]:
        """Yield every stored record."""
        if not self.path.exists():
            return
        for line in self._read_lines():
            yield json.loads(line)


class Manifest:
    """Which harvest queries ran to completion, so a re-run does not spend budget twice.

    Stored as JSON next to the stores: ``{query_key: {count, n_records, finished_at}}``.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.entries: dict[str, dict[str, Any]] = {}
        if self.path.exists():
            self.entries = json.loads(self.path.read_text(encoding="utf-8"))

    def is_done(self, key: str) -> bool:
        return key in self.entries

    def mark_done(self, key: str, **info: Any) -> None:
        self.entries[key] = {**info, "finished_at": datetime.now(timezone.utc).isoformat()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.entries, indent=2), encoding="utf-8")

    def forget(self, prefix: str = "") -> None:
        """Drop completion marks whose key starts with ``prefix`` (all of them by default)."""
        self.entries = {k: v for k, v in self.entries.items() if not k.startswith(prefix)}
        self.path.write_text(json.dumps(self.entries, indent=2), encoding="utf-8")
