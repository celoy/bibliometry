import gzip

import httpx
import pytest

from bibliometry.openalex import (
    BudgetExhaustedError,
    OpenAlexClient,
    author_works_filter,
    batched,
    institution_works_filter,
    reconstruct_abstract,
    short_id,
)
from bibliometry.store import Manifest, WorksStore


def test_store_deduplicates_and_resumes(tmp_path):
    path = tmp_path / "works.jsonl.gz"
    store = WorksStore(path)
    assert (
        store.add_many([{"id": "https://openalex.org/W1"}, {"id": "https://openalex.org/W2"}]) == 2
    )
    assert (
        store.add_many([{"id": "https://openalex.org/W2"}, {"id": "https://openalex.org/W3"}]) == 1
    )
    reopened = WorksStore(path)
    assert reopened.ids == {"W1", "W2", "W3"}
    assert [short_id(r["id"]) for r in reopened.iter_records()] == ["W1", "W2", "W3"]


def test_store_recovers_from_truncated_tail(tmp_path):
    path = tmp_path / "works.jsonl.gz"
    WorksStore(path).add_many([{"id": "https://openalex.org/W1"}])
    with path.open("ab") as handle:  # a killed run: half a gzip member
        handle.write(gzip.compress(b'{"id": "https://openalex.org/W2"}\n')[:15])
    with pytest.warns(UserWarning, match="recovered 1"):
        store = WorksStore(path)
    assert store.ids == {"W1"}
    assert WorksStore(path).ids == {"W1"}  # file was repaired


def test_manifest(tmp_path):
    manifest = Manifest(tmp_path / "m.json")
    manifest.mark_done("institution:I1:2020:2021", count=3)
    assert Manifest(tmp_path / "m.json").is_done("institution:I1:2020:2021")
    manifest.forget("institution")
    assert not Manifest(tmp_path / "m.json").is_done("institution:I1:2020:2021")


def test_filters_and_helpers():
    assert institution_works_filter("I1", "2020-01-01", "2020-12-31") == (
        "authorships.institutions.lineage:I1,from_publication_date:2020-01-01,"
        "to_publication_date:2020-12-31"
    )
    assert author_works_filter(["A1", "A2"], "a", "b").startswith("author.id:A1|A2,")
    assert [len(c) for c in batched(map(str, range(120)))] == [50, 50, 20]
    assert reconstruct_abstract({"world": [1], "Hello": [0]}) == "Hello world"
    assert reconstruct_abstract(None) is None


def _client(handler) -> OpenAlexClient:
    client = OpenAlexClient(
        mailto="a@b.org", api_key="k", min_interval=0, transport=httpx.MockTransport(handler)
    )
    client.backoff = 0
    return client


def test_cursor_paging_sends_identity():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        cursor = request.url.params["cursor"]
        if cursor == "*":
            return httpx.Response(200, json={"meta": {"next_cursor": "c2"}, "results": [{"id": 1}]})
        return httpx.Response(200, json={"meta": {"next_cursor": None}, "results": [{"id": 2}]})

    with _client(handler) as client:
        assert [r["id"] for r in client.iter_records("/works", "x:1")] == [1, 2]
    assert seen[0]["mailto"] == "a@b.org" and seen[0]["api_key"] == "k"


def test_spent_budget_fails_fast():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            429,
            headers={
                "x-ratelimit-remaining-usd": "0",
                "x-ratelimit-limit-usd": "0.1",
                "x-ratelimit-reset": "3600",
            },
            json={"message": "budget exceeded"},
        )

    with _client(handler) as client, pytest.raises(BudgetExhaustedError, match="resume"):
        client.get("/works")
    assert len(calls) == 1


def test_transient_errors_are_retried():
    responses = iter(
        [httpx.Response(503), httpx.Response(429), httpx.Response(200, json={"ok": 1})]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    with _client(handler) as client:
        assert client.get("/works") == {"ok": 1}
        assert client.n_requests == 3
