"""
ARKAINBRAIN — ACP Admin Decorators

Access control: Only ADMIN_EMAIL can access the Control Plane.
Set ADMIN_EMAIL in .env or Railway environment variables.
Step-up auth for dangerous operations.
"""

import json
import os
import uuid
import logging
from datetime import datetime
from functools import wraps
from flask import session, redirect, request, g, jsonify

logger = logging.getLogger("arkainbrain.admin")

# ── Admin email — set in .env or Railway env vars ──
# Only this email can access /admin/ routes
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip().lower()

ROLES = {
    "viewer": {"level": 1, "perms": ["read"]},
    "editor": {"level": 2, "perms": ["read", "write"]},
    "admin":  {"level": 3, "perms": ["read", "write", "dangerous"]},
}

PLANS = {
    "free":       {"label": "Free",       "price": 0,    "monthly_jobs": 10},
    "pro":        {"label": "Pro",        "price": 49,   "monthly_jobs": 100},
    "studio":     {"label": "Studio",     "price": 199,  "monthly_jobs": 500},
    "enterprise": {"label": "Enterprise", "price": None, "monthly_jobs": 99999},
}


def _current_user():
    return session.get("user", {})


def _is_admin(user: dict) -> bool:
    """Check if user is the admin. Matches against ADMIN_EMAIL env var."""
    if not user or not user.get("email"):
        return False
    if not ADMIN_EMAIL:
        # No admin email configured — block everyone with helpful message
        logger.warning("ADMIN_EMAIL not set — Control Plane locked")
        return False
    return user["email"].strip().lower() == ADMIN_EMAIL


def admin_required(f):
    """Require authenticated admin (email must match ADMIN_EMAIL)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        user = _current_user()
        if not user or not user.get("id"):
            return redirect("/login")
        if not _is_admin(user):
            return (
                f'<div style="padding:40px;text-align:center;font-family:sans-serif">'
                f'<h2>🔒 Control Plane — Access Denied</h2>'
                f'<p style="color:#888">Your email ({user.get("email","?")}) is not authorized.</p>'
                f'<p style="color:#666;font-size:13px">Set <code>ADMIN_EMAIL</code> in environment variables to grant access.</p>'
                f'<a href="/" style="color:#7c6aef">← Back to Dashboard</a></div>'
            ), 403
        return f(*args, **kwargs)
    return decorated


def role_required(min_role="viewer"):
    """Require minimum role level. Admin email always passes."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            user = _current_user()
            if not user or not user.get("id"):
                return redirect("/login")
            # Admin email bypasses role checks
            if _is_admin(user):
                return f(*args, **kwargs)
            user_role = user.get("role", "viewer")
            user_level = ROLES.get(user_role, {}).get("level", 0)
            required_level = ROLES.get(min_role, {}).get("level", 99)
            if user_level < required_level:
                return f"Forbidden — requires {min_role} role", 403
            return f(*args, **kwargs)
        return decorated
    return decorator


def stepup_required(f):
    """Require step-up confirmation for dangerous actions."""
    @wraps(f)
    def decorated(*args, **kwargs):
        confirm = request.form.get("confirm_stepup") or request.args.get("confirm_stepup")
        if not confirm:
            return jsonify({"error": "Step-up confirmation required",
                            "requires_stepup": True}), 403
        return f(*args, **kwargs)
    return decorated


def audit_log(action, target_type=None, target_id=None, details=None):
    """Record an admin action in the audit log."""
    try:
        from config.database import get_db
        user = _current_user()
        db = get_db()
        db.execute(
            "INSERT INTO admin_audit_log (id, admin_id, action, target_type, target_id, "
            "details, ip_address, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4())[:12], user.get("id", "system"), action,
             target_type, target_id,
             json.dumps(details) if details else None,
             request.remote_addr if request else None,
             datetime.now().isoformat())
        )
        db.commit()
    except Exception as e:
        logger.warning(f"Audit log failed: {e}")


def get_plan_info(plan_name):
    return PLANS.get(plan_name, PLANS["free"])
