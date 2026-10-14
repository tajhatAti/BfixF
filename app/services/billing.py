"""Plans, orders, coupons, trials and revenue helpers (business layer)."""
from __future__ import annotations

import time

from app.logger import log
from app.services import access, settings
from app.storage import db
from app.utils import money


PAYMENT_LABELS = (("PAY_BKASH", "bKash"), ("PAY_NAGAD", "Nagad"),
                  ("PAY_ROCKET", "Rocket"), ("PAY_UPI", "UPI"),
                  ("PAY_CRYPTO", "Crypto"))


def payment_methods() -> list[tuple[str, str]]:
    """(label, value) pairs saved by the admin — from the bot or the website."""
    methods = []
    for key, label in PAYMENT_LABELS:
        value = str(settings.get(key, "") or "").strip()
        if value:
            methods.append((label, value))
    return methods


def methods_text() -> str:
    methods = payment_methods()
    if not methods:
        return ""
    return "\n".join(f"• <b>{name}</b>: <code>{value}</code>" for name, value in methods)


def price_text(amount: float) -> str:
    return money(amount, settings.get_str("CURRENCY", "৳") or "৳")


def plan_label(plan: dict) -> str:
    return f"{plan['name']} — {price_text(plan['price'])}"


def default_plans(store_id: int) -> list[dict]:
    """Sensible starter plans so a new store can sell immediately."""
    if db.plans(store_id, only_active=True):
        return db.plans(store_id, only_active=True)
    return []


def store_sales(store_id: int) -> dict:
    orders = [o for o in db.orders(limit=500) if o["store_id"] == store_id]
    approved = [o for o in orders if o["status"] == "approved"]
    pending = [o for o in orders if o["status"] == "pending"]
    return {
        "total_orders": len(orders),
        "approved": len(approved),
        "pending": len(pending),
        "revenue": sum(o["amount"] or 0 for o in approved),
    }


def create_order_from_plan(user_id: int, store_id: int, plan: dict, method: str,
                           discount_percent: int = 0, coupon: str | None = None) -> dict:
    """Create a pending payment request. Free plans are auto-approved."""
    amount = float(plan["price"] or 0)
    if discount_percent:
        amount = round(amount * (100 - discount_percent) / 100, 2)

    note = f"coupon:{coupon}" if coupon else ""
    order_id = db.create_order(
        user_id=user_id, store_id=store_id, plan=plan, method=method,
        amount=amount, currency=settings.get_str("CURRENCY", "৳"), note=note,
    )

    note_order_started(user_id, store_id, int(plan.get("id") or 0), amount)
    if amount <= 0 and settings.get_bool("AUTO_APPROVE_ZERO", True):
        approve_order(order_id, admin_id=0, note="auto (free plan)")
        log.info("Order %s auto-approved (free plan) for user %s", order_id, user_id)

    return db.order(order_id)


def approve_order(order_id: int, admin_id: int, note: str = "") -> dict | None:
    """Grant the plan's access and mark the order approved."""
    order = db.order(order_id)
    if order is None or order["status"] == "approved":
        return order

    store = db.store(order["store_id"])
    days = int(order["days"] or 0)
    if store and days:
        access.grant_days(store["id"], order["user_id"], days, source="payment")
    elif store:
        access.grant_access(store["id"], order["user_id"], None, source="payment")

    db.decide_order(order_id, "approved", admin_id, note)
    if order["note"] and order["note"].startswith("coupon:"):
        db.use_coupon(order["note"].split(":", 1)[1])
    db.log_event("paid", order["user_id"], order["store_id"], None,
                 f"order:{order_id}", f"days={days}")
    log.info("Order %s approved by %s", order_id, admin_id)
    return db.order(order_id)


def note_order_started(user_id: int, store_id: int, plan_id: int, amount: float) -> None:
    """Analytics: the user reached the payment step (funnel “came → paid”)."""
    db.log_event("pay_start", user_id, store_id, None, f"plan:{plan_id}",
                 f"amount={amount:.0f}")


def reject_order(order_id: int, admin_id: int, note: str = "") -> dict | None:
    db.decide_order(order_id, "rejected", admin_id, note)
    log.info("Order %s rejected by %s", order_id, admin_id)
    return db.order(order_id)


def trial_hours() -> int:
    """Free trial length, as saved in the panel (0 = trials off)."""
    return settings.get_int("TRIAL_HOURS", 0)


def grant_trial(user_id: int, store: dict) -> bool:
    """One free trial per user per store (length from the panel setting)."""
    hours = trial_hours()
    if hours <= 0:
        return False
    if db.trial_used(user_id, store["id"]):
        return False
    access.grant_access(store["id"], user_id,
                        time.time() + hours * 3600, source="trial")
    db.mark_trial(user_id, store["id"])
    return True


def trial_available(user_id: int, store: dict) -> bool:
    return trial_hours() > 0 and not db.trial_used(user_id, store["id"])


def apply_coupon(code: str, user_id: int, store: dict, plan: dict | None = None) -> tuple[bool, str]:
    """Validate a coupon. Percent coupons return the discounted price, day
    coupons unlock access straight away."""
    ok, reason = db.coupon_valid(code, store["id"])
    if not ok:
        return False, reason

    coupon = db.coupon(code)
    if coupon["days"]:
        access.grant_days(store["id"], user_id, coupon["days"], source="coupon")
        db.use_coupon(coupon["code"])
        return True, f"days:{coupon['days']}"

    if plan is None:
        return False, "Coupon needs a plan selected."
    new_price = round(float(plan["price"]) * (100 - coupon["percent"]) / 100, 2)
    return True, f"percent:{coupon['percent']}:{new_price}"


def upsell_text(store: dict, user_id: int) -> str:
    """Short pitch appended to locked-store screens."""
    plans = db.plans(store["id"], only_active=True)
    if not plans:
        return ""
    cheapest = min(plans, key=lambda p: p["price"])
    lines = ["", "💎 <b>Available plans</b>"]
    for plan in plans[:4]:
        lines.append(f"• {plan['name']} — {price_text(plan['price'])}")
    lines.append(f"<i>Starting from {price_text(cheapest['price'])}</i>")
    return "\n".join(lines)
