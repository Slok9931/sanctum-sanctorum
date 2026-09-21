"""Member operations and tier helpers."""
from datetime import datetime
from typing import List

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Member, MemberTier, Order, OrderStatus
from app.schemas import MemberCreate, MemberStats

# Tiers from lowest to highest; a member's rank is their index in this list.
TIER_ORDER: List[str] = [
    MemberTier.APPRENTICE.value,
    MemberTier.ADEPT.value,
    MemberTier.MASTER.value,
    MemberTier.SUPREME.value,
]

# Minimum tier allowed to buy or borrow restricted books.
RESTRICTED_MIN_TIER = MemberTier.MASTER.value


def tier_at_least(tier: str, minimum: str) -> bool:
    """True if ``tier`` ranks at or above ``minimum``."""
    return TIER_ORDER.index(tier) >= TIER_ORDER.index(minimum)


def ensure_can_access_restricted(member: Member) -> None:
    """Raise 403 unless the member's tier may access restricted books."""
    if not tier_at_least(member.tier, RESTRICTED_MIN_TIER):
        raise HTTPException(
            status_code=403, detail=f"Restricted books require tier '{RESTRICTED_MIN_TIER}' or higher"
        )


def create_member(db: Session, data: MemberCreate, now: datetime) -> Member:
    """Register a member.

    Rules: email (already stripped + lowercased) must be unique -> 409; created_at = now.
    """
    existing = db.scalar(select(Member).where(func.lower(Member.email) == data.email))
    if existing is not None:
        raise HTTPException(status_code=409, detail="A member with this email already exists")
    member = Member(name=data.name, email=data.email, tier=data.tier.value, created_at=now)
    db.add(member)
    db.commit()
    db.refresh(member)
    return member


def get_member(db: Session, member_id: int) -> Member:
    """Return a member by id, or raise 404."""
    member = db.get(Member, member_id)
    if member is None:
        raise HTTPException(status_code=404, detail="Member not found")
    return member


def list_member_orders(db: Session, member_id: int) -> List[Order]:
    """All orders of a member ordered by id ascending; 404 if the member is missing."""
    get_member(db, member_id)
    return list(db.scalars(select(Order).where(Order.member_id == member_id).order_by(Order.id)))


def get_member_stats(db: Session, member_id: int, now: datetime) -> MemberStats:
    member = get_member(db, member_id)
    orders_paid, total_spent_cents = 0, 0
    for order in member.orders:
        if order.status == OrderStatus.PAID.value:
            orders_paid += 1
            total_spent_cents += order.total_cents
    active_loans, overdue_loans, late_fees_cents = 0, 0, 0
    for loan in member.loans:
        if loan.returned_at is None:
            active_loans += 1
            if now > loan.due_at:
                overdue_loans += 1
        else:
            late_fees_cents += loan.late_fee_cents
    return MemberStats(
        member_id=member.id, orders_paid=orders_paid, total_spent_cents=total_spent_cents,
        active_loans=active_loans, overdue_loans=overdue_loans, late_fees_cents=late_fees_cents,
    )
