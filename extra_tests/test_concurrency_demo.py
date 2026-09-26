"""Optional extra: demonstrate that concurrent orders for the last copy of a book
cannot oversell stock.

Deliberately kept OUTSIDE tests/ rather than added into it: INSTRUCTIONS.md says not to
modify anything in tests/, and adding a new file there felt like the same spirit even
though it's not literally "modifying" an existing file. Run it the same way:

    uv run pytest extra_tests/ -v

What this proves: app/services/orders.py used to (and a naive implementation still would)
read `book.stock` in Python, check it, then write `book.stock - quantity` back. Two
concurrent requests for the very last copy can both read stock=1 before either commits,
both pass the check, and both decrement -> stock ends at -1, oversold.

The fix (see create_order) replaces the final decrement with a single conditional UPDATE:
    UPDATE books SET stock = stock - :qty WHERE id = :id AND stock >= :qty
evaluated by the database at write time, not from a value read earlier in Python. Only one
of two racing requests can have its WHERE clause still be true; the other gets rowcount 0
and the whole order is rolled back. This test fires many concurrent requests at a book with
exactly one copy in stock and asserts exactly one of them wins.
"""
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401  (registers tables on Base.metadata)
from app.clock import get_now
from app.db import Base, get_db
from app.main import create_app

START = datetime(2026, 1, 1, 12, 0, 0)
CONCURRENT_REQUESTS = 12


def isbn13(seed: int) -> str:
    body = f"978{seed:09d}"
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(body))
    return body + str((10 - total % 10) % 10)


@pytest.fixture
def client():
    # A real *file-based* SQLite database, not the usual in-memory "sqlite://" from
    # tests/conftest.py. In-memory SQLite with StaticPool shares ONE connection object
    # across every thread, and two threads issuing BEGIN/COMMIT on that same shared
    # connection at once corrupts its transaction bookkeeping (a SQLite driver
    # limitation, not an application bug) -- exactly what you'd hit if you naively
    # pointed a real concurrency test at the default test fixture. A file-based
    # database uses the default pool, so each thread's session gets its own real
    # connection, and SQLite's own file locking serializes the actual writes --
    # which is what we're trying to exercise here.
    tmp_path = Path(tempfile.mkstemp(suffix=".db")[1])
    engine = create_engine(f"sqlite:///{tmp_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    application = create_app(init_db=False)
    application.dependency_overrides[get_db] = override_get_db
    application.dependency_overrides[get_now] = lambda: START
    with TestClient(application) as test_client:
        yield test_client
    engine.dispose()
    tmp_path.unlink(missing_ok=True)


def test_only_one_concurrent_order_wins_the_last_copy(client):
    book = client.post(
        "/books",
        json={
            "title": "The Last Copy",
            "author": "A. Uthor",
            "isbn": isbn13(1),
            "price_cents": 1000,
            "stock": 1,  # exactly one copy -- everyone is racing for the same unit
            "restricted": False,
        },
    ).json()

    member_ids = []
    for i in range(CONCURRENT_REQUESTS):
        member = client.post(
            "/members", json={"name": f"Racer {i}", "email": f"racer{i}@example.com"}
        ).json()
        member_ids.append(member["id"])

    # A barrier forces every thread to fire its request at (as close to) the same instant
    # as possible, maximizing the chance of two requests actually interleaving mid-flight
    # instead of running one cleanly after another.
    barrier = threading.Barrier(CONCURRENT_REQUESTS)

    def place_order(member_id: int):
        barrier.wait()
        response = client.post(
            "/orders",
            json={"member_id": member_id, "items": [{"book_id": book["id"], "quantity": 1}]},
        )
        return response.status_code

    with ThreadPoolExecutor(max_workers=CONCURRENT_REQUESTS) as pool:
        results = list(pool.map(place_order, member_ids))

    successes = [code for code in results if code == 201]
    conflicts = [code for code in results if code == 409]

    assert len(successes) == 1, f"expected exactly 1 winner, got {len(successes)}: {results}"
    assert len(conflicts) == CONCURRENT_REQUESTS - 1

    final_stock = client.get(f"/books/{book['id']}").json()["stock"]
    assert final_stock == 0, f"stock must land at exactly 0, never negative; got {final_stock}"