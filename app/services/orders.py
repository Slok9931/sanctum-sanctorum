"""Order operations: placing, paying and cancelling purchases."""
from datetime import datetime
from typing import Dict

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import Member, MemberTier, Order, OrderStatus, Book, OrderItem
from app.schemas import OrderCreate
from app.services.members import ensure_can_access_restricted

# Percentage discount granted by each membership tier.
TIER_DISCOUNT_PERCENT: Dict[str, int] = {
    MemberTier.APPRENTICE.value: 0,
    MemberTier.ADEPT.value: 5,
    MemberTier.MASTER.value: 10,
    MemberTier.SUPREME.value: 15,
}

# Extra discount when the total quantity across all items reaches the threshold.
BULK_QUANTITY_THRESHOLD = 10
BULK_DISCOUNT_PERCENT = 5


def calculate_discount_percent(member: Member, total_quantity: int) -> int:
    percent = TIER_DISCOUNT_PERCENT[member.tier]
    if total_quantity >= BULK_QUANTITY_THRESHOLD:
        percent += BULK_DISCOUNT_PERCENT
    return percent


def create_order(db: Session, data: OrderCreate, now: datetime) -> Order:
    member = db.get(Member, data.member_id)
    if member is None:
        raise HTTPException(status_code=404, detail="Member not found")

    books: Dict[int, Book] = {}
    for item in data.items:
        book = db.get(Book, item.book_id)
        if book is None:
            raise HTTPException(status_code=404, detail=f"Book {item.book_id} not found")
        books[item.book_id] = book

    for item in data.items:
        if books[item.book_id].restricted:
            ensure_can_access_restricted(member)

    # All stock is checked BEFORE any is touched — this is what "all-or-nothing" means.
    for item in data.items:
        book = books[item.book_id]
        if book.stock < item.quantity:
            raise HTTPException(status_code=409, detail=f"Insufficient stock for book {item.book_id}")

    order_items, subtotal_cents, total_quantity = [], 0, 0
    for item in data.items:
        book = books[item.book_id]
        book.stock -= item.quantity
        subtotal_cents += book.price_cents * item.quantity
        total_quantity += item.quantity
        order_items.append(OrderItem(book_id=book.id, quantity=item.quantity, unit_price_cents=book.price_cents))

    discount_percent = calculate_discount_percent(member, total_quantity)
    discount_cents = subtotal_cents * discount_percent // 100
    total_cents = subtotal_cents - discount_cents

    order = Order(
        member_id=member.id, status=OrderStatus.PENDING.value, subtotal_cents=subtotal_cents,
        discount_percent=discount_percent, discount_cents=discount_cents, total_cents=total_cents,
        created_at=now, items=order_items,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def get_order(db: Session, order_id: int) -> Order:
    """Return an order by id, or raise 404."""
    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


def pay_order(db: Session, order_id: int) -> Order:
    """Mark a pending order as paid. 404 if missing; 409 if not pending."""
    order = get_order(db, order_id)
    if order.status != OrderStatus.PENDING.value:
        raise HTTPException(status_code=409, detail=f"Cannot pay an order that is {order.status}")
    order.status = OrderStatus.PAID.value
    db.commit()
    db.refresh(order)
    return order


def cancel_order(db: Session, order_id: int) -> Order:
    """Cancel a pending order and restore the reserved stock. 404 if missing; 409 if not pending."""
    order = get_order(db, order_id)
    if order.status != OrderStatus.PENDING.value:
        raise HTTPException(status_code=409, detail=f"Cannot cancel an order that is {order.status}")
    order.status = OrderStatus.CANCELLED.value
    db.commit()
    db.refresh(order)
    return order
