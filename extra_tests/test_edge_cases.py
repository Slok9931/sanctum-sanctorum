"""Optional extra: a few edge cases the required suite in tests/ doesn't cover.

Kept outside tests/ for the same reason as test_concurrency_demo.py -- INSTRUCTIONS.md says
not to modify anything in tests/, and this file is not part of the graded acceptance
criteria. Run with:

    uv run pytest extra_tests/test_edge_cases.py -v

The required suite (tests/) is genuinely thorough -- most edge cases I first thought of
(all-or-nothing on insufficient stock, restricted-book ordering, PATCH silently dropping
isbn, offset past the end of a page, quantity <= 0) turned out to already be covered there.
These three are the gaps I actually found:
  1. GET /members pagination -- an optional extra I added myself, so naturally the required
     suite has no tests for it at all.
  2. An ISBN-13 whose check digit computes to exactly 0 -- (10 - total % 10) % 10 has a
     special case at total % 10 == 0, and it's easy to get that arithmetic subtly wrong
     (off by one in the modulo) without a test that actually forces it.
  3. A mixed order (one normal book + one restricted book) where the member is denied --
     the required suite checks all-or-nothing for insufficient *stock* across two books,
     and 403 stock-safety for a *single* restricted book, but not both combined: does a
     403 on item 2 correctly leave item 1's stock untouched too?
"""
import pytest

from tests.conftest import isbn13


def place_order(client, member_id, *items):
    """items: (book_id, quantity) pairs. Local copy of the helper from tests/test_orders.py."""
    body = {
        "member_id": member_id,
        "items": [{"book_id": book_id, "quantity": quantity} for book_id, quantity in items],
    }
    return client.post("/orders", json=body)


def stock_of(client, book):
    return client.get(f"/books/{book['id']}").json()["stock"]


class TestMemberPagination:
    """GET /members -- the optional extra endpoint, has zero coverage in tests/."""

    def test_empty_store(self, client):
        response = client.get("/members")
        assert response.status_code == 200
        assert response.json() == {"items": [], "total": 0, "limit": 20, "offset": 0}

    def test_default_pagination(self, client, make_member):
        for _ in range(3):
            make_member()
        body = client.get("/members").json()
        assert body["total"] == 3
        assert len(body["items"]) == 3
        assert body["limit"] == 20
        assert body["offset"] == 0

    def test_limit_and_offset(self, client, make_member):
        members = [make_member() for _ in range(5)]
        body = client.get("/members", params={"limit": 2, "offset": 2}).json()
        assert body["total"] == 5
        assert [m["id"] for m in body["items"]] == [members[2]["id"], members[3]["id"]]

    def test_offset_past_end_returns_no_items_but_total(self, client, make_member):
        make_member()
        body = client.get("/members", params={"offset": 50}).json()
        assert body["items"] == []
        assert body["total"] == 1

    def test_ordered_by_id_ascending(self, client, make_member):
        members = [make_member() for _ in range(4)]
        body = client.get("/members").json()
        assert [m["id"] for m in body["items"]] == [m["id"] for m in members]

    @pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1}])
    def test_out_of_range_params_return_422(self, client, params):
        assert client.get("/members", params=params).status_code == 422


class TestIsbnChecksumEdgeCase:
    def test_check_digit_of_exactly_zero_is_accepted(self, client):
        # Find a 12-digit body (within isbn13's own numbering scheme) whose checksum
        # naturally computes to 0, rather than hand-editing a valid ISBN's last digit
        # (which risks accidentally testing "checksum is invalid" instead).
        for seed in range(1, 200):
            candidate = isbn13(seed)
            if candidate[-1] == "0":
                break
        else:
            pytest.skip("no seed in range produced a check digit of 0")

        response = client.post(
            "/books",
            json={
                "title": "Zero Check Digit",
                "author": "Someone",
                "isbn": candidate,
                "price_cents": 500,
                "stock": 1,
                "restricted": False,
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["isbn"] == candidate


class TestMixedRestrictedOrderAllOrNothing:
    def test_403_on_second_item_leaves_first_items_stock_untouched(
        self, client, make_member, make_book
    ):
        allowed = make_book(stock=10, restricted=False)
        restricted = make_book(stock=10, restricted=True)
        member = make_member(tier="apprentice")

        response = place_order(client, member["id"], (allowed["id"], 3), (restricted["id"], 1))

        assert response.status_code == 403
        assert stock_of(client, allowed) == 10, "allowed item's stock must not be touched"
        assert stock_of(client, restricted) == 10
        assert client.get(f"/members/{member['id']}/orders").json() == []