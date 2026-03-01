"""
ARKAINBRAIN — ACP Site Administration Routes

5 pages for full site management:
  1. Users     — manage accounts, roles, plans, suspend/unsuspend
  2. Jobs      — monitor all pipeline jobs, logs, kill/retry
  3. Games     — browse all generated games, download, delete
  4. System    — DB stats, disk, env, health, error log
  5. Settings  — global config, API keys, maintenance mode
"""

import json
import os
import shutil
import sqlite3
import time
import platform
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from flask import request, jsonify, session, redirect, send_file

from admin import admin_bp
from admin.decorators import admin_required, stepup_required, audit_log
from admin.routes import acp_layout, _esc, _get_acp

_BASE = Path(__file__).resolve().parent.parent


# ═══════════════════════════════════════════════════════════
#  Shared helpers
# ═══════════════════════════════════════════════════════════

def _db():
    from config.database import get_db
    return get_db()


def _q(db, sql, params=()):
    """Safe query — returns list of dicts."""
    try:
        rows = db.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _q1(db, sql, params=()):
    rows = _q(db, sql, params)
    return rows[0] if rows else None


def _ago(iso_str):
    """Friendly time-ago from ISO timestamp."""
    if not iso_str:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00").split("+")[0])
        delta = datetime.now() - dt
        if delta.days > 365:
            return f"{delta.days // 365}y ago"
        if delta.days > 30:
            return f"{delta.days // 30}mo ago"
        if delta.days > 0:
            return f"{delta.days}d ago"
        hours = delta.seconds // 3600
        if hours > 0:
            return f"{hours}h ago"
        mins = delta.seconds // 60
        return f"{max(1, mins)}m ago"
    except Exception:
        return iso_str[:16] if iso_str else "—"


def _badge(text, variant="active"):
    return f'<span class="badge badge-{variant}">{_esc(text)}</span>'


def _plan_badge(plan):
    colors = {"free": "inactive", "pro": "active", "studio": "warn", "enterprise": "accent"}
    return _badge(plan or "free", colors.get(plan, "inactive"))


def _status_badge(status):
    colors = {"completed": "active", "running": "warn", "queued": "inactive",
              "failed": "danger", "cancelled": "danger"}
    return _badge(status or "unknown", colors.get(status, "inactive"))


# ═══════════════════════════════════════════════════════════
#  Page 8: Users Management
# ═══════════════════════════════════════════════════════════

@admin_bp.route("/users")
@admin_required
def acp_users():
    db = _db()
    search = request.args.get("q", "").strip()
    plan_filter = request.args.get("plan", "")
    role_filter = request.args.get("role", "")
    page = max(1, int(request.args.get("page", 1)))
    per_page = 25

    where, params = ["1=1"], []
    if search:
        where.append("(u.email LIKE ? OR u.name LIKE ? OR u.id LIKE ?)")
        params += [f"%{search}%"] * 3
    if plan_filter:
        where.append("u.plan = ?")
        params.append(plan_filter)
    if role_filter:
        where.append("u.role = ?")
        params.append(role_filter)

    where_sql = " AND ".join(where)
    total = _q1(db, f"SELECT COUNT(*) as c FROM users u WHERE {where_sql}", params)
    total_count = total["c"] if total else 0
    offset = (page - 1) * per_page

    users = _q(db, f"""
        SELECT u.*, 
               (SELECT COUNT(*) FROM jobs j WHERE j.user_id=u.id) as job_count,
               (SELECT COUNT(*) FROM jobs j WHERE j.user_id=u.id AND j.status='completed') as completed_count
        FROM users u WHERE {where_sql}
        ORDER BY u.created_at DESC LIMIT ? OFFSET ?
    """, params + [per_page, offset])

    # Aggregate stats
    stats = _q1(db, """
        SELECT COUNT(*) as total,
               SUM(CASE WHEN plan='pro' THEN 1 ELSE 0 END) as pro,
               SUM(CASE WHEN plan='studio' THEN 1 ELSE 0 END) as studio,
               SUM(CASE WHEN plan='enterprise' THEN 1 ELSE 0 END) as enterprise,
               SUM(CASE WHEN is_suspended=1 THEN 1 ELSE 0 END) as suspended,
               SUM(CASE WHEN created_at > datetime('now','-7 days') THEN 1 ELSE 0 END) as new_7d
        FROM users
    """) or {}

    rows_html = ""
    for u in users:
        susp = ' style="opacity:0.5"' if u.get("is_suspended") else ""
        rows_html += f"""<tr{susp}>
            <td><div style="display:flex;align-items:center;gap:8px">
                <img src="{_esc(u.get('picture',''))}" width="24" height="24" style="border-radius:50%;background:#333" onerror="this.style.display='none'">
                <div><div style="font-weight:600">{_esc(u.get('name','?'))}</div>
                     <div style="font-size:10px;color:var(--dim)">{_esc(u.get('email',''))}</div></div>
            </div></td>
            <td>{_plan_badge(u.get('plan'))}</td>
            <td><span style="font-size:10px;color:var(--dim)">{_esc(u.get('role','user'))}</span></td>
            <td style="font-variant-numeric:tabular-nums">{u.get('job_count',0)} <span style="color:var(--dim)">({u.get('completed_count',0)}✓)</span></td>
            <td style="font-size:11px">{u.get('monthly_jobs_used',0)}/{u.get('monthly_job_limit',10)}</td>
            <td style="font-size:11px;color:var(--dim)">{_ago(u.get('last_active_at') or u.get('created_at'))}</td>
            <td>
                {'<span class="badge badge-danger">SUSPENDED</span>' if u.get('is_suspended') else ''}
                <a href="/admin/users/{u['id']}" class="btn btn-outline btn-sm">Edit</a>
            </td>
        </tr>"""

    total_pages = max(1, -(-total_count // per_page))
    pager = ""
    if total_pages > 1:
        for p in range(1, total_pages + 1):
            active = " btn-primary" if p == page else " btn-outline"
            pager += f'<a href="/admin/users?page={p}&q={_esc(search)}&plan={_esc(plan_filter)}" class="btn btn-sm{active}">{p}</a> '

    content = f"""
    <div class="page-title">👥 User Management</div>
    <div class="page-sub">{total_count} users total · Manage accounts, plans, and access</div>

    <div class="stat-row">
        <div class="stat-box"><div class="val">{stats.get('total',0)}</div><div class="lbl">Total Users</div></div>
        <div class="stat-box ok"><div class="val">{stats.get('new_7d',0)}</div><div class="lbl">New (7d)</div></div>
        <div class="stat-box"><div class="val">{(stats.get('pro',0) or 0) + (stats.get('studio',0) or 0) + (stats.get('enterprise',0) or 0)}</div><div class="lbl">Paid Plans</div></div>
        <div class="stat-box {'danger' if stats.get('suspended',0) else ''}""><div class="val">{stats.get('suspended',0)}</div><div class="lbl">Suspended</div></div>
    </div>

    <div class="card">
        <form method="get" style="display:flex;gap:8px;margin-bottom:12px">
            <input type="text" name="q" value="{_esc(search)}" placeholder="Search email, name, or ID..." style="flex:1">
            <select name="plan" style="width:120px"><option value="">All Plans</option>
                <option value="free" {'selected' if plan_filter=='free' else ''}>Free</option>
                <option value="pro" {'selected' if plan_filter=='pro' else ''}>Pro</option>
                <option value="studio" {'selected' if plan_filter=='studio' else ''}>Studio</option>
                <option value="enterprise" {'selected' if plan_filter=='enterprise' else ''}>Enterprise</option>
            </select>
            <button class="btn btn-primary" type="submit">Search</button>
        </form>
        <table>
            <thead><tr><th>User</th><th>Plan</th><th>Role</th><th>Jobs</th><th>Quota</th><th>Active</th><th></th></tr></thead>
            <tbody>{rows_html}</tbody>
        </table>
        <div style="margin-top:12px;display:flex;gap:4px">{pager}</div>
    </div>
    """
    return acp_layout(content, "users")


@admin_bp.route("/users/<uid>", methods=["GET", "POST"])
@admin_required
def acp_user_detail(uid):
    db = _db()
    user = _q1(db, "SELECT * FROM users WHERE id=?", (uid,))
    if not user:
        return "User not found", 404

    msg = ""
    if request.method == "POST":
        action = request.form.get("action")
        if action == "update_role":
            new_role = request.form.get("role", "user")
            db.execute("UPDATE users SET role=? WHERE id=?", (new_role, uid))
            db.commit()
            audit_log("user.role_change", "user", uid, {"new_role": new_role})
            msg = f'<div style="color:var(--success);font-size:12px;margin:8px 0">✓ Role updated to {new_role}</div>'
        elif action == "update_plan":
            new_plan = request.form.get("plan", "free")
            limits = {"free": 10, "pro": 100, "studio": 500, "enterprise": 99999}
            db.execute("UPDATE users SET plan=?, monthly_job_limit=?, plan_started_at=? WHERE id=?",
                       (new_plan, limits.get(new_plan, 10), datetime.now().isoformat(), uid))
            db.commit()
            audit_log("user.plan_change", "user", uid, {"new_plan": new_plan})
            msg = f'<div style="color:var(--success);font-size:12px;margin:8px 0">✓ Plan updated to {new_plan}</div>'
        elif action == "reset_quota":
            db.execute("UPDATE users SET monthly_jobs_used=0 WHERE id=?", (uid,))
            db.commit()
            audit_log("user.quota_reset", "user", uid)
            msg = '<div style="color:var(--success);font-size:12px;margin:8px 0">✓ Monthly quota reset</div>'
        elif action == "suspend":
            reason = request.form.get("reason", "Admin action")
            db.execute("UPDATE users SET is_suspended=1, suspension_reason=? WHERE id=?", (reason, uid))
            db.commit()
            audit_log("user.suspend", "user", uid, {"reason": reason})
            msg = '<div style="color:var(--danger);font-size:12px;margin:8px 0">⚠ User suspended</div>'
        elif action == "unsuspend":
            db.execute("UPDATE users SET is_suspended=0, suspension_reason=NULL WHERE id=?", (uid,))
            db.commit()
            audit_log("user.unsuspend", "user", uid)
            msg = '<div style="color:var(--success);font-size:12px;margin:8px 0">✓ User unsuspended</div>'
        user = _q1(db, "SELECT * FROM users WHERE id=?", (uid,))

    jobs = _q(db, """SELECT id, title, job_type, status, created_at, completed_at 
                      FROM jobs WHERE user_id=? ORDER BY created_at DESC LIMIT 20""", (uid,))
    jobs_html = ""
    for j in jobs:
        jobs_html += f"""<tr>
            <td><a href="/admin/jobs/{j['id']}" style="color:var(--accent)">{_esc(j['id'][:8])}</a></td>
            <td>{_esc(j.get('title',''))[:40]}</td>
            <td>{_esc(j.get('job_type',''))}</td>
            <td>{_status_badge(j.get('status'))}</td>
            <td style="font-size:11px;color:var(--dim)">{_ago(j.get('created_at'))}</td>
        </tr>"""

    is_susp = user.get("is_suspended")
    _suspend_btn = '<form method="post" style="display:inline;margin-left:8px"><input type="hidden" name="action" value="suspend"><input type="hidden" name="reason" value="Admin action"><button class="btn btn-danger btn-sm" onclick="return confirm(&#39;Suspend this user?&#39;)">Suspend</button></form>'
    content = f"""
    <div class="page-title">👤 {_esc(user.get('name','Unknown'))}</div>
    <div class="page-sub">{_esc(user.get('email',''))} · Joined {_ago(user.get('created_at'))}</div>
    {msg}

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
        <div class="card">
            <h3>Account Details</h3>
            <table>
                <tr><td style="color:var(--dim);width:120px">ID</td><td><code>{_esc(uid)}</code></td></tr>
                <tr><td style="color:var(--dim)">Email</td><td>{_esc(user.get('email',''))}</td></tr>
                <tr><td style="color:var(--dim)">Provider</td><td>{_esc(user.get('provider','google'))}</td></tr>
                <tr><td style="color:var(--dim)">Plan</td><td>{_plan_badge(user.get('plan'))}</td></tr>
                <tr><td style="color:var(--dim)">Role</td><td>{_esc(user.get('role','user'))}</td></tr>
                <tr><td style="color:var(--dim)">Quota</td><td>{user.get('monthly_jobs_used',0)} / {user.get('monthly_job_limit',10)}</td></tr>
                <tr><td style="color:var(--dim)">Status</td><td>{'<span class="badge badge-danger">SUSPENDED</span> ' + _esc(user.get("suspension_reason","")) if is_susp else '<span class="badge badge-active">Active</span>'}</td></tr>
                <tr><td style="color:var(--dim)">Created</td><td>{_esc(user.get('created_at','')[:19])}</td></tr>
                <tr><td style="color:var(--dim)">Last Active</td><td>{_ago(user.get('last_active_at'))}</td></tr>
            </table>
        </div>

        <div class="card">
            <h3>Actions</h3>
            <form method="post" style="margin-bottom:10px">
                <input type="hidden" name="action" value="update_role">
                <div class="form-field"><label>Role</label>
                <select name="role"><option value="user" {'selected' if user.get('role')=='user' else ''}>User</option>
                <option value="editor" {'selected' if user.get('role')=='editor' else ''}>Editor</option>
                <option value="admin" {'selected' if user.get('role')=='admin' else ''}>Admin</option></select></div>
                <button class="btn btn-primary btn-sm">Update Role</button>
            </form>
            <form method="post" style="margin-bottom:10px">
                <input type="hidden" name="action" value="update_plan">
                <div class="form-field"><label>Plan</label>
                <select name="plan"><option value="free" {'selected' if user.get('plan')=='free' else ''}>Free (10/mo)</option>
                <option value="pro" {'selected' if user.get('plan')=='pro' else ''}>Pro (100/mo)</option>
                <option value="studio" {'selected' if user.get('plan')=='studio' else ''}>Studio (500/mo)</option>
                <option value="enterprise" {'selected' if user.get('plan')=='enterprise' else ''}>Enterprise (∞)</option></select></div>
                <button class="btn btn-primary btn-sm">Update Plan</button>
            </form>
            <form method="post" style="display:inline"><input type="hidden" name="action" value="reset_quota">
                <button class="btn btn-outline btn-sm">Reset Monthly Quota</button></form>
            {'<form method="post" style="display:inline;margin-left:8px"><input type="hidden" name="action" value="unsuspend"><button class="btn btn-success btn-sm">Unsuspend</button></form>' if is_susp else ''}
            {_suspend_btn if not is_susp else ''}
        </div>
    </div>

    <div class="card" style="margin-top:16px">
        <h3>Recent Jobs ({len(jobs)})</h3>
        <table><thead><tr><th>ID</th><th>Title</th><th>Type</th><th>Status</th><th>Created</th></tr></thead>
        <tbody>{jobs_html if jobs_html else '<tr><td colspan="5" style="color:var(--dim)">No jobs yet</td></tr>'}</tbody></table>
    </div>
    <a href="/admin/users" class="btn btn-outline" style="margin-top:12px">← Back to Users</a>
    """
    return acp_layout(content, "users")


# ═══════════════════════════════════════════════════════════
#  Page 9: Jobs Monitor
# ═══════════════════════════════════════════════════════════

@admin_bp.route("/jobs")
@admin_required
def acp_jobs():
    db = _db()
    search = request.args.get("q", "").strip()
    status_f = request.args.get("status", "")
    type_f = request.args.get("type", "")
    page = max(1, int(request.args.get("page", 1)))
    per_page = 30

    where, params = ["1=1"], []
    if search:
        where.append("(j.title LIKE ? OR j.id LIKE ? OR u.email LIKE ?)")
        params += [f"%{search}%"] * 3
    if status_f:
        where.append("j.status=?")
        params.append(status_f)
    if type_f:
        where.append("j.job_type=?")
        params.append(type_f)

    where_sql = " AND ".join(where)
    total = _q1(db, f"SELECT COUNT(*) as c FROM jobs j LEFT JOIN users u ON j.user_id=u.id WHERE {where_sql}", params)
    total_count = total["c"] if total else 0
    offset = (page - 1) * per_page

    jobs = _q(db, f"""
        SELECT j.*, u.email as user_email, u.name as user_name
        FROM jobs j LEFT JOIN users u ON j.user_id=u.id
        WHERE {where_sql} ORDER BY j.created_at DESC LIMIT ? OFFSET ?
    """, params + [per_page, offset])

    stats = _q1(db, """
        SELECT COUNT(*) as total,
               SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) as running,
               SUM(CASE WHEN status='queued' THEN 1 ELSE 0 END) as queued,
               SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as completed,
               SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed,
               SUM(CASE WHEN created_at > datetime('now','-24 hours') THEN 1 ELSE 0 END) as today
        FROM jobs
    """) or {}

    rows_html = ""
    for j in jobs:
        dur = ""
        if j.get("completed_at") and j.get("created_at"):
            try:
                t0 = datetime.fromisoformat(j["created_at"].split("+")[0])
                t1 = datetime.fromisoformat(j["completed_at"].split("+")[0])
                secs = (t1 - t0).total_seconds()
                dur = f"{secs/60:.1f}m" if secs > 60 else f"{secs:.0f}s"
            except Exception:
                pass
        rows_html += f"""<tr>
            <td><a href="/admin/jobs/{j['id']}" style="color:var(--accent);font-family:var(--mono);font-size:11px">{_esc(j['id'][:10])}</a></td>
            <td style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{_esc(j.get('title',''))}</td>
            <td><span style="font-size:10px;color:var(--dim)">{_esc(j.get('job_type',''))}</span></td>
            <td>{_status_badge(j.get('status'))}</td>
            <td style="font-size:10px;color:var(--dim)">{_esc(j.get('current_stage','')[:25])}</td>
            <td style="font-size:11px">{_esc(j.get('user_email','')[:25])}</td>
            <td style="font-size:11px;color:var(--dim)">{_ago(j.get('created_at'))}</td>
            <td style="font-size:11px;font-family:var(--mono)">{dur}</td>
        </tr>"""

    total_pages = max(1, -(-total_count // per_page))
    pager = ""
    if total_pages > 1:
        for p in range(1, min(total_pages + 1, 12)):
            active = " btn-primary" if p == page else " btn-outline"
            pager += f'<a href="/admin/jobs?page={p}&q={_esc(search)}&status={_esc(status_f)}&type={_esc(type_f)}" class="btn btn-sm{active}">{p}</a> '

    content = f"""
    <div class="page-title">🔄 Jobs Monitor</div>
    <div class="page-sub">{total_count} total jobs · Real-time pipeline monitoring</div>

    <div class="stat-row">
        <div class="stat-box"><div class="val">{stats.get('total',0)}</div><div class="lbl">Total Jobs</div></div>
        <div class="stat-box warn"><div class="val">{stats.get('running',0)}</div><div class="lbl">Running</div></div>
        <div class="stat-box"><div class="val">{stats.get('queued',0)}</div><div class="lbl">Queued</div></div>
        <div class="stat-box ok"><div class="val">{stats.get('completed',0)}</div><div class="lbl">Completed</div></div>
        <div class="stat-box danger"><div class="val">{stats.get('failed',0)}</div><div class="lbl">Failed</div></div>
        <div class="stat-box"><div class="val">{stats.get('today',0)}</div><div class="lbl">Last 24h</div></div>
    </div>

    <div class="card">
        <form method="get" style="display:flex;gap:8px;margin-bottom:12px">
            <input type="text" name="q" value="{_esc(search)}" placeholder="Search title, ID, or email..." style="flex:1">
            <select name="status" style="width:120px"><option value="">All Status</option>
                <option value="running" {'selected' if status_f=='running' else ''}>Running</option>
                <option value="queued" {'selected' if status_f=='queued' else ''}>Queued</option>
                <option value="completed" {'selected' if status_f=='completed' else ''}>Completed</option>
                <option value="failed" {'selected' if status_f=='failed' else ''}>Failed</option></select>
            <select name="type" style="width:140px"><option value="">All Types</option>
                <option value="slot_pipeline" {'selected' if type_f=='slot_pipeline' else ''}>Slot Pipeline</option>
                <option value="mini_game" {'selected' if type_f=='mini_game' else ''}>Mini Game</option>
                <option value="novel_game" {'selected' if type_f=='novel_game' else ''}>Novel Game</option></select>
            <button class="btn btn-primary" type="submit">Filter</button>
        </form>
        <table>
            <thead><tr><th>ID</th><th>Title</th><th>Type</th><th>Status</th><th>Stage</th><th>User</th><th>Created</th><th>Duration</th></tr></thead>
            <tbody>{rows_html}</tbody>
        </table>
        <div style="margin-top:12px;display:flex;gap:4px">{pager}</div>
    </div>
    """
    return acp_layout(content, "jobs")


@admin_bp.route("/jobs/<jid>", methods=["GET", "POST"])
@admin_required
def acp_job_detail(jid):
    db = _db()
    job = _q1(db, """SELECT j.*, u.email as user_email, u.name as user_name
                      FROM jobs j LEFT JOIN users u ON j.user_id=u.id WHERE j.id=?""", (jid,))
    if not job:
        return "Job not found", 404

    msg = ""
    if request.method == "POST":
        action = request.form.get("action")
        if action == "cancel":
            db.execute("UPDATE jobs SET status='cancelled', error='Cancelled by admin' WHERE id=?", (jid,))
            db.commit()
            audit_log("job.cancel", "job", jid)
            msg = '<div style="color:var(--danger);font-size:12px;margin:8px 0">⚠ Job cancelled</div>'
        elif action == "retry":
            db.execute("UPDATE jobs SET status='queued', error=NULL, current_stage='Retrying' WHERE id=?", (jid,))
            db.commit()
            audit_log("job.retry", "job", jid)
            msg = '<div style="color:var(--success);font-size:12px;margin:8px 0">✓ Job re-queued</div>'
        elif action == "delete":
            # Delete output directory
            out_dir = job.get("output_dir")
            if out_dir and Path(out_dir).exists():
                shutil.rmtree(out_dir, ignore_errors=True)
            db.execute("DELETE FROM reviews WHERE job_id=?", (jid,))
            db.execute("DELETE FROM jobs WHERE id=?", (jid,))
            db.commit()
            audit_log("job.delete", "job", jid)
            return redirect("/admin/jobs")
        job = _q1(db, "SELECT j.*, u.email as user_email FROM jobs j LEFT JOIN users u ON j.user_id=u.id WHERE j.id=?", (jid,))

    # Load log file if exists
    log_content = ""
    log_path = _BASE / "logs" / f"{jid}.log"
    if log_path.exists():
        try:
            lines = log_path.read_text(errors="replace").split("\n")
            last_100 = "\n".join(lines[-100:])
            log_content = f'<pre style="background:#0a0b10;padding:12px;border-radius:6px;font-size:11px;max-height:300px;overflow-y:auto;color:#94a3b8;white-space:pre-wrap">{_esc(last_100)}</pre>'
        except Exception:
            pass

    # Reviews
    reviews = _q(db, "SELECT * FROM reviews WHERE job_id=? ORDER BY created_at", (jid,))
    reviews_html = ""
    for r in reviews:
        reviews_html += f'<tr><td>{_esc(r.get("stage",""))}</td><td>{_esc(r.get("title",""))}</td><td>{_status_badge(r.get("status"))}</td><td>{_ago(r.get("created_at"))}</td></tr>'

    params_str = ""
    try:
        p = json.loads(job.get("params", "{}") or "{}")
        params_str = json.dumps(p, indent=2)
    except Exception:
        params_str = job.get("params", "")

    _cancel_btn = '<form method="post" style="display:inline"><input type="hidden" name="action" value="cancel"><button class="btn btn-danger btn-sm" onclick="return confirm(&#39;Cancel this job?&#39;)">Cancel Job</button></form>'

    content = f"""
    <div class="page-title">📋 Job: {_esc(job.get('title',''))[:50]}</div>
    <div class="page-sub">{_esc(jid)} · {_status_badge(job.get('status'))} · {_esc(job.get('job_type',''))}</div>
    {msg}

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
        <div class="card">
            <h3>Details</h3>
            <table>
                <tr><td style="color:var(--dim);width:110px">User</td><td>{_esc(job.get('user_email',''))}</td></tr>
                <tr><td style="color:var(--dim)">Type</td><td>{_esc(job.get('job_type',''))}</td></tr>
                <tr><td style="color:var(--dim)">Status</td><td>{_status_badge(job.get('status'))}</td></tr>
                <tr><td style="color:var(--dim)">Stage</td><td>{_esc(job.get('current_stage',''))}</td></tr>
                <tr><td style="color:var(--dim)">Created</td><td>{_esc(job.get('created_at','')[:19])}</td></tr>
                <tr><td style="color:var(--dim)">Completed</td><td>{_esc(job.get('completed_at','') or '—')}</td></tr>
                <tr><td style="color:var(--dim)">Output</td><td style="font-size:10px;word-break:break-all">{_esc(job.get('output_dir','') or '—')}</td></tr>
                {f'<tr><td style="color:var(--danger)">Error</td><td style="color:var(--danger);font-size:11px">{_esc(job.get("error",""))}</td></tr>' if job.get('error') else ''}
            </table>
        </div>
        <div class="card">
            <h3>Actions</h3>
            <div style="display:flex;gap:8px;flex-wrap:wrap">
                {_cancel_btn if job.get('status') in ('running','queued') else ''}
                {'<form method="post" style="display:inline"><input type="hidden" name="action" value="retry"><button class="btn btn-primary btn-sm">Retry Job</button></form>' if job.get('status') in ('failed','cancelled') else ''}
                <form method="post" style="display:inline"><input type="hidden" name="action" value="delete">
                    <button class="btn btn-danger btn-sm" onclick="return confirm('Delete job and all outputs?')">Delete Job</button></form>
                <a href="/admin/users/{job.get('user_id','')}" class="btn btn-outline btn-sm">View User</a>
            </div>
            <h3 style="margin-top:16px">Parameters</h3>
            <pre style="background:#0a0b10;padding:10px;border-radius:6px;font-size:10px;max-height:200px;overflow-y:auto;color:#94a3b8">{_esc(params_str[:2000])}</pre>
        </div>
    </div>

    {f'<div class="card" style="margin-top:16px"><h3>Log Output (last 100 lines)</h3>{log_content}</div>' if log_content else ''}

    {'<div class="card" style="margin-top:16px"><h3>Reviews (' + str(len(reviews)) + ')</h3><table><thead><tr><th>Stage</th><th>Title</th><th>Status</th><th>Created</th></tr></thead><tbody>' + reviews_html + '</tbody></table></div>' if reviews else ''}

    <a href="/admin/jobs" class="btn btn-outline" style="margin-top:12px">← Back to Jobs</a>
    """
    return acp_layout(content, "jobs")


# ═══════════════════════════════════════════════════════════
#  Page 10: Games Library
# ═══════════════════════════════════════════════════════════

@admin_bp.route("/games")
@admin_required
def acp_games():
    db = _db()

    # Generated mini-games
    gen_dir = _BASE / "static" / "arcade" / "games" / "generated"
    gen_games = []
    registry_path = gen_dir / "_registry.json"
    if registry_path.exists():
        try:
            gen_games = json.loads(registry_path.read_text())
        except Exception:
            pass

    # DB-tracked generated games
    db_games = _q(db, """
        SELECT g.*, u.email as user_email 
        FROM generated_games g LEFT JOIN users u ON g.user_id=u.id 
        ORDER BY g.created_at DESC LIMIT 100
    """)

    # Slot prototypes from completed jobs
    prototypes = _q(db, """
        SELECT j.id, j.title, j.status, j.output_dir, j.created_at, u.email as user_email
        FROM jobs j LEFT JOIN users u ON j.user_id=u.id
        WHERE j.job_type='slot_pipeline' AND j.status='completed'
        ORDER BY j.created_at DESC LIMIT 50
    """)

    # Phase 2/3 games
    phase_dirs = [
        (_BASE / "static" / "arcade" / "games" / "phase2", "Phase 2"),
        (_BASE / "static" / "arcade" / "games" / "phase3", "Phase 3"),
    ]
    phase_games = []
    for d, label in phase_dirs:
        if d.exists():
            for f in sorted(d.glob("*.html")):
                phase_games.append({"file": f.name, "phase": label, "size": f.stat().st_size})

    # Build cards
    gen_html = ""
    for g in gen_games[:30]:
        gen_html += f"""<div class="pt-card" style="text-align:left">
            <div style="font-weight:700;font-size:12px">{_esc(g.get('theme_name','') or g.get('id',''))}</div>
            <div style="font-size:10px;color:var(--dim)">{_esc(g.get('game_type',''))} · {_ago(g.get('created_at',''))}</div>
        </div>"""

    proto_html = ""
    for p in prototypes[:20]:
        has_html = False
        if p.get("output_dir"):
            proto_path = Path(p["output_dir"]) / "07_prototype" / "index.html"
            has_html = proto_path.exists()
        proto_html += f"""<tr>
            <td>{_esc(p.get('title','')[:40])}</td>
            <td>{_esc(p.get('user_email','')[:25])}</td>
            <td>{_ago(p.get('created_at'))}</td>
            <td>{'<span class="badge badge-active">Has Demo</span>' if has_html else '<span class="badge badge-inactive">No Demo</span>'}</td>
        </tr>"""

    phase_html = ""
    for pg in phase_games:
        size_kb = pg["size"] / 1024
        phase_html += f"""<tr>
            <td>{_esc(pg['file'])}</td>
            <td>{_esc(pg['phase'])}</td>
            <td>{size_kb:.0f}KB</td>
        </tr>"""

    content = f"""
    <div class="page-title">🎮 Games Library</div>
    <div class="page-sub">{len(gen_games)} generated · {len(prototypes)} prototypes · {len(phase_games)} built-in</div>

    <div class="stat-row">
        <div class="stat-box"><div class="val">{len(gen_games)}</div><div class="lbl">Generated Games</div></div>
        <div class="stat-box"><div class="val">{len(prototypes)}</div><div class="lbl">Slot Prototypes</div></div>
        <div class="stat-box"><div class="val">{len(phase_games)}</div><div class="lbl">Built-in Games</div></div>
        <div class="stat-box"><div class="val">{len(db_games)}</div><div class="lbl">DB Tracked</div></div>
    </div>

    <div class="card">
        <h3>Slot Prototypes</h3>
        <table><thead><tr><th>Title</th><th>User</th><th>Created</th><th>Demo</th></tr></thead>
        <tbody>{proto_html if proto_html else '<tr><td colspan="4" style="color:var(--dim)">No prototypes yet</td></tr>'}</tbody></table>
    </div>

    <div class="card">
        <h3>Generated Mini-Games ({len(gen_games)})</h3>
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px">
            {gen_html if gen_html else '<div style="color:var(--dim);font-size:12px">No generated games yet</div>'}
        </div>
    </div>

    <div class="card">
        <h3>Built-in Games ({len(phase_games)})</h3>
        <table><thead><tr><th>File</th><th>Phase</th><th>Size</th></tr></thead>
        <tbody>{phase_html}</tbody></table>
    </div>
    """
    return acp_layout(content, "games")


# ═══════════════════════════════════════════════════════════
#  Page 11: System Health
# ═══════════════════════════════════════════════════════════

@admin_bp.route("/system")
@admin_required
def acp_system():
    db = _db()

    # DB stats
    db_path = os.environ.get("DATABASE_PATH", "arkainbrain.db")
    db_size = 0
    try:
        db_size = os.path.getsize(db_path) if os.path.exists(db_path) else 0
    except Exception:
        pass
    db_size_mb = db_size / (1024 * 1024)

    tables = _q(db, "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    table_stats = []
    for t in tables:
        name = t["name"]
        try:
            count = _q1(db, f"SELECT COUNT(*) as c FROM [{name}]")
            table_stats.append({"name": name, "rows": count["c"] if count else 0})
        except Exception:
            table_stats.append({"name": name, "rows": "?"})

    # Disk usage
    disk_usage = shutil.disk_usage("/")
    disk_total_gb = disk_usage.total / (1024**3)
    disk_used_gb = disk_usage.used / (1024**3)
    disk_free_gb = disk_usage.free / (1024**3)
    disk_pct = (disk_usage.used / disk_usage.total) * 100

    # Output directory size
    output_size = 0
    output_base = _BASE / "output"
    if output_base.exists():
        for f in output_base.rglob("*"):
            if f.is_file():
                output_size += f.stat().st_size
    output_mb = output_size / (1024 * 1024)

    # Static/games size
    games_size = 0
    games_dir = _BASE / "static" / "arcade" / "games"
    if games_dir.exists():
        for f in games_dir.rglob("*"):
            if f.is_file():
                games_size += f.stat().st_size
    games_mb = games_size / (1024 * 1024)

    # System info
    py_ver = platform.python_version()
    os_info = f"{platform.system()} {platform.release()}"
    hostname = platform.node()

    # Environment keys (masked)
    env_keys = []
    important_vars = ["ADMIN_EMAIL", "GOOGLE_CLIENT_ID", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                      "DATABASE_PATH", "FLASK_SECRET_KEY", "RAILWAY_ENVIRONMENT",
                      "QDRANT_URL", "QDRANT_API_KEY", "SMTP_HOST", "SMTP_USER"]
    for var in important_vars:
        val = os.environ.get(var, "")
        if val:
            masked = val[:4] + "****" + val[-4:] if len(val) > 8 else "****"
            env_keys.append({"key": var, "value": masked, "set": True})
        else:
            env_keys.append({"key": var, "value": "NOT SET", "set": False})

    # Error log (last errors from jobs)
    recent_errors = _q(db, """
        SELECT id, title, error, created_at FROM jobs 
        WHERE status='failed' AND error IS NOT NULL 
        ORDER BY created_at DESC LIMIT 10
    """)

    table_rows = ""
    for ts in sorted(table_stats, key=lambda x: x["rows"] if isinstance(x["rows"], int) else 0, reverse=True):
        table_rows += f'<tr><td style="font-family:var(--mono);font-size:11px">{_esc(ts["name"])}</td><td style="text-align:right;font-variant-numeric:tabular-nums">{ts["rows"]}</td></tr>'

    env_rows = ""
    for ev in env_keys:
        color = "var(--success)" if ev["set"] else "var(--danger)"
        env_rows += f'<tr><td style="font-family:var(--mono);font-size:11px">{_esc(ev["key"])}</td><td style="color:{color};font-size:11px">{_esc(ev["value"])}</td></tr>'

    error_rows = ""
    for er in recent_errors:
        error_rows += f'<tr><td><a href="/admin/jobs/{er["id"]}" style="color:var(--accent);font-size:11px">{_esc(er["id"][:8])}</a></td><td style="font-size:11px">{_esc(er.get("title","")[:30])}</td><td style="font-size:10px;color:var(--danger)">{_esc(er.get("error","")[:80])}</td><td style="font-size:10px;color:var(--dim)">{_ago(er.get("created_at"))}</td></tr>'

    disk_class = "danger" if disk_pct > 90 else ("warn" if disk_pct > 75 else "ok")

    content = f"""
    <div class="page-title">🖥 System Health</div>
    <div class="page-sub">Server diagnostics, storage, and environment</div>

    <div class="stat-row">
        <div class="stat-box"><div class="val">{db_size_mb:.1f}MB</div><div class="lbl">Database</div></div>
        <div class="stat-box {disk_class}"><div class="val">{disk_pct:.0f}%</div><div class="lbl">Disk Used</div></div>
        <div class="stat-box"><div class="val">{output_mb:.0f}MB</div><div class="lbl">Output Files</div></div>
        <div class="stat-box"><div class="val">{games_mb:.0f}MB</div><div class="lbl">Game Files</div></div>
        <div class="stat-box"><div class="val">{len(tables)}</div><div class="lbl">DB Tables</div></div>
        <div class="stat-box"><div class="val">{len(recent_errors)}</div><div class="lbl">Recent Errors</div></div>
    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
        <div class="card">
            <h3>Server Info</h3>
            <table>
                <tr><td style="color:var(--dim)">Python</td><td>{py_ver}</td></tr>
                <tr><td style="color:var(--dim)">OS</td><td>{os_info}</td></tr>
                <tr><td style="color:var(--dim)">Host</td><td>{hostname}</td></tr>
                <tr><td style="color:var(--dim)">Disk</td><td>{disk_used_gb:.1f}GB / {disk_total_gb:.1f}GB ({disk_free_gb:.1f}GB free)</td></tr>
                <tr><td style="color:var(--dim)">Database</td><td>{db_path} ({db_size_mb:.1f}MB)</td></tr>
            </table>
        </div>
        <div class="card">
            <h3>Environment Variables</h3>
            <table>{env_rows}</table>
        </div>
    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:16px">
        <div class="card">
            <h3>Database Tables</h3>
            <table><thead><tr><th>Table</th><th style="text-align:right">Rows</th></tr></thead>
            <tbody>{table_rows}</tbody></table>
        </div>
        <div class="card">
            <h3>Recent Errors</h3>
            <table><thead><tr><th>Job</th><th>Title</th><th>Error</th><th>When</th></tr></thead>
            <tbody>{error_rows if error_rows else '<tr><td colspan="4" style="color:var(--success)">No recent errors 🎉</td></tr>'}</tbody></table>
        </div>
    </div>

    <div class="card" style="margin-top:16px">
        <h3>Maintenance Actions</h3>
        <div style="display:flex;gap:8px">
            <button class="btn btn-outline btn-sm" onclick="fetch('/admin/api/system/vacuum',{{method:'POST'}}).then(r=>r.json()).then(d=>{{alert(d.message);location.reload();}})">🧹 Vacuum Database</button>
            <button class="btn btn-outline btn-sm" onclick="fetch('/admin/api/system/clear-stale',{{method:'POST'}}).then(r=>r.json()).then(d=>{{alert(d.message);location.reload();}})">🗑 Clear Stale Jobs</button>
            <button class="btn btn-danger btn-sm" onclick="if(confirm('Clear ALL output files?'))fetch('/admin/api/system/clear-outputs',{{method:'POST'}}).then(r=>r.json()).then(d=>{{alert(d.message);location.reload();}})">⚠ Clear Output Files</button>
        </div>
    </div>
    """
    return acp_layout(content, "system")


# ═══════════════════════════════════════════════════════════
#  Page 12: Global Settings
# ═══════════════════════════════════════════════════════════

@admin_bp.route("/settings", methods=["GET", "POST"])
@admin_required
def acp_settings():
    acp, db = _get_acp()

    msg = ""
    if request.method == "POST":
        section = request.form.get("section")
        if section == "general":
            for key in ["site_name", "site_tagline", "maintenance_mode", "signup_enabled",
                        "max_concurrent_jobs", "default_plan", "job_timeout_minutes"]:
                val = request.form.get(key, "")
                acp.set_setting(f"site.{key}", val, user_id=session.get("user", {}).get("id", "admin"))
            msg = '<div style="color:var(--success);font-size:12px;margin:8px 0">✓ General settings saved</div>'
        elif section == "api_keys":
            for key in ["openai_api_key", "anthropic_api_key", "qdrant_url", "qdrant_api_key"]:
                val = request.form.get(key, "").strip()
                if val and val != "****":
                    acp.set_setting(f"api.{key}", val, user_id=session.get("user", {}).get("id", "admin"))
            msg = '<div style="color:var(--success);font-size:12px;margin:8px 0">✓ API keys saved</div>'
        elif section == "email":
            for key in ["smtp_host", "smtp_port", "smtp_user", "smtp_pass", "smtp_from", "smtp_tls"]:
                val = request.form.get(key, "").strip()
                if val and val != "****":
                    acp.set_setting(f"email.{key}", val, user_id=session.get("user", {}).get("id", "admin"))
            msg = '<div style="color:var(--success);font-size:12px;margin:8px 0">✓ Email settings saved</div>'
        elif section == "llm":
            for key in ["default_model", "temperature", "max_tokens", "provider"]:
                val = request.form.get(key, "").strip()
                if val:
                    acp.set_setting(f"llm.{key}", val, user_id=session.get("user", {}).get("id", "admin"))
            msg = '<div style="color:var(--success);font-size:12px;margin:8px 0">✓ LLM settings saved</div>'
        audit_log("settings.update", "settings", section)

    # Load current settings
    def gs(key, default=""):
        return acp.get_setting(key, default)

    content = f"""
    <div class="page-title">⚙️ Global Settings</div>
    <div class="page-sub">Platform configuration · Changes apply immediately</div>
    {msg}

    <div class="tab-bar">
        <a href="#general" class="active" onclick="showTab('general',this)">General</a>
        <a href="#api" onclick="showTab('api',this)">API Keys</a>
        <a href="#email" onclick="showTab('email',this)">Email / SMTP</a>
        <a href="#llm" onclick="showTab('llm',this)">LLM Config</a>
    </div>

    <div id="tab-general" class="card">
        <form method="post">
            <input type="hidden" name="section" value="general">
            <div class="form-row">
                <div class="form-field"><label>Site Name</label>
                    <input type="text" name="site_name" value="{_esc(gs('site.site_name', 'ArkainBrain'))}"></div>
                <div class="form-field"><label>Tagline</label>
                    <input type="text" name="site_tagline" value="{_esc(gs('site.site_tagline', 'AI Game Studio'))}"></div>
            </div>
            <div class="form-row">
                <div class="form-field"><label>Default Plan</label>
                    <select name="default_plan">
                        <option value="free" {'selected' if gs('site.default_plan','free')=='free' else ''}>Free</option>
                        <option value="pro" {'selected' if gs('site.default_plan')=='pro' else ''}>Pro</option>
                    </select></div>
                <div class="form-field"><label>Max Concurrent Jobs</label>
                    <input type="number" name="max_concurrent_jobs" value="{_esc(gs('site.max_concurrent_jobs', '3'))}"></div>
            </div>
            <div class="form-row">
                <div class="form-field"><label>Job Timeout (minutes)</label>
                    <input type="number" name="job_timeout_minutes" value="{_esc(gs('site.job_timeout_minutes', '30'))}"></div>
                <div class="form-field"><label>Maintenance Mode</label>
                    <select name="maintenance_mode">
                        <option value="0" {'selected' if gs('site.maintenance_mode','0')=='0' else ''}>Off</option>
                        <option value="1" {'selected' if gs('site.maintenance_mode')=='1' else ''}>On — Block new jobs</option>
                    </select></div>
            </div>
            <div class="form-row">
                <div class="form-field"><label>Signups Enabled</label>
                    <select name="signup_enabled">
                        <option value="1" {'selected' if gs('site.signup_enabled','1')=='1' else ''}>Yes</option>
                        <option value="0" {'selected' if gs('site.signup_enabled')=='0' else ''}>No — Invite only</option>
                    </select></div>
                <div class="form-field"></div>
            </div>
            <button class="btn btn-primary">Save General Settings</button>
        </form>
    </div>

    <div id="tab-api" class="card" style="display:none">
        <form method="post">
            <input type="hidden" name="section" value="api_keys">
            <div class="form-field"><label>OpenAI API Key</label>
                <input type="text" name="openai_api_key" placeholder="{_esc(gs('api.openai_api_key','NOT SET')[:4])}****" value=""></div>
            <div class="form-field"><label>Anthropic API Key</label>
                <input type="text" name="anthropic_api_key" placeholder="{_esc(gs('api.anthropic_api_key','NOT SET')[:4])}****" value=""></div>
            <div class="form-row">
                <div class="form-field"><label>Qdrant URL</label>
                    <input type="text" name="qdrant_url" value="{_esc(gs('api.qdrant_url',''))}"></div>
                <div class="form-field"><label>Qdrant API Key</label>
                    <input type="text" name="qdrant_api_key" placeholder="****" value=""></div>
            </div>
            <button class="btn btn-primary">Save API Keys</button>
            <span style="font-size:10px;color:var(--dim);margin-left:8px">Leave blank to keep existing value</span>
        </form>
    </div>

    <div id="tab-email" class="card" style="display:none">
        <form method="post">
            <input type="hidden" name="section" value="email">
            <div class="form-row">
                <div class="form-field"><label>SMTP Host</label>
                    <input type="text" name="smtp_host" value="{_esc(gs('email.smtp_host',''))}"></div>
                <div class="form-field"><label>SMTP Port</label>
                    <input type="number" name="smtp_port" value="{_esc(gs('email.smtp_port','587'))}"></div>
            </div>
            <div class="form-row">
                <div class="form-field"><label>SMTP User</label>
                    <input type="text" name="smtp_user" value="{_esc(gs('email.smtp_user',''))}"></div>
                <div class="form-field"><label>SMTP Password</label>
                    <input type="text" name="smtp_pass" placeholder="****" value=""></div>
            </div>
            <div class="form-row">
                <div class="form-field"><label>From Address</label>
                    <input type="text" name="smtp_from" value="{_esc(gs('email.smtp_from',''))}"></div>
                <div class="form-field"><label>Use TLS</label>
                    <select name="smtp_tls">
                        <option value="1" {'selected' if gs('email.smtp_tls','1')=='1' else ''}>Yes</option>
                        <option value="0" {'selected' if gs('email.smtp_tls')=='0' else ''}>No</option>
                    </select></div>
            </div>
            <button class="btn btn-primary">Save Email Settings</button>
        </form>
    </div>

    <div id="tab-llm" class="card" style="display:none">
        <form method="post">
            <input type="hidden" name="section" value="llm">
            <div class="form-row">
                <div class="form-field"><label>Provider</label>
                    <select name="provider">
                        <option value="openai" {'selected' if gs('llm.provider','openai')=='openai' else ''}>OpenAI</option>
                        <option value="anthropic" {'selected' if gs('llm.provider')=='anthropic' else ''}>Anthropic</option>
                    </select></div>
                <div class="form-field"><label>Default Model</label>
                    <input type="text" name="default_model" value="{_esc(gs('llm.default_model','gpt-4.1-2025-04-14'))}"></div>
            </div>
            <div class="form-row">
                <div class="form-field"><label>Temperature</label>
                    <input type="text" name="temperature" value="{_esc(gs('llm.temperature','0.7'))}"></div>
                <div class="form-field"><label>Max Tokens</label>
                    <input type="number" name="max_tokens" value="{_esc(gs('llm.max_tokens','4096'))}"></div>
            </div>
            <button class="btn btn-primary">Save LLM Config</button>
        </form>
    </div>

    <script>
    function showTab(id, el) {{
        document.querySelectorAll('[id^=tab-]').forEach(t=>t.style.display='none');
        document.getElementById('tab-'+id).style.display='block';
        document.querySelectorAll('.tab-bar a').forEach(a=>a.classList.remove('active'));
        el.classList.add('active');
    }}
    </script>
    """
    return acp_layout(content, "settings")


# ═══════════════════════════════════════════════════════════
#  API Endpoints — Site Admin
# ═══════════════════════════════════════════════════════════

@admin_bp.route("/api/users/<uid>/suspend", methods=["POST"])
@admin_required
def api_suspend_user(uid):
    db = _db()
    reason = request.json.get("reason", "Admin action") if request.is_json else "Admin action"
    db.execute("UPDATE users SET is_suspended=1, suspension_reason=? WHERE id=?", (reason, uid))
    db.commit()
    audit_log("user.suspend", "user", uid, {"reason": reason})
    return jsonify({"ok": True})


@admin_bp.route("/api/users/<uid>/unsuspend", methods=["POST"])
@admin_required
def api_unsuspend_user(uid):
    db = _db()
    db.execute("UPDATE users SET is_suspended=0, suspension_reason=NULL WHERE id=?", (uid,))
    db.commit()
    audit_log("user.unsuspend", "user", uid)
    return jsonify({"ok": True})


@admin_bp.route("/api/jobs/<jid>/cancel", methods=["POST"])
@admin_required
def api_cancel_job(jid):
    db = _db()
    db.execute("UPDATE jobs SET status='cancelled', error='Cancelled by admin' WHERE id=?", (jid,))
    db.commit()
    audit_log("job.cancel", "job", jid)
    return jsonify({"ok": True})


@admin_bp.route("/api/jobs/<jid>/retry", methods=["POST"])
@admin_required
def api_retry_job(jid):
    db = _db()
    db.execute("UPDATE jobs SET status='queued', error=NULL, current_stage='Retrying' WHERE id=?", (jid,))
    db.commit()
    audit_log("job.retry", "job", jid)
    return jsonify({"ok": True})


@admin_bp.route("/api/system/vacuum", methods=["POST"])
@admin_required
def api_vacuum():
    db = _db()
    try:
        db.execute("VACUUM")
        audit_log("system.vacuum", "system")
        return jsonify({"ok": True, "message": "Database vacuumed successfully"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@admin_bp.route("/api/system/clear-stale", methods=["POST"])
@admin_required
def api_clear_stale():
    db = _db()
    cutoff = (datetime.now() - timedelta(hours=6)).isoformat()
    result = db.execute(
        "UPDATE jobs SET status='failed', error='Stale — cleared by admin' "
        "WHERE status IN ('running','queued') AND created_at < ?", (cutoff,)
    )
    db.commit()
    count = result.rowcount
    audit_log("system.clear_stale", "system", details={"cleared": count})
    return jsonify({"ok": True, "message": f"Cleared {count} stale jobs"})


@admin_bp.route("/api/system/clear-outputs", methods=["POST"])
@admin_required
def api_clear_outputs():
    output_dir = _BASE / "output"
    cleared = 0
    if output_dir.exists():
        for item in output_dir.iterdir():
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
                cleared += 1
    audit_log("system.clear_outputs", "system", details={"cleared": cleared})
    return jsonify({"ok": True, "message": f"Cleared {cleared} output directories"})


@admin_bp.route("/api/users/export", methods=["GET"])
@admin_required
def api_export_users():
    db = _db()
    users = _q(db, "SELECT id, email, name, plan, role, monthly_jobs_used, created_at, last_active_at, is_suspended FROM users ORDER BY created_at DESC")
    return jsonify(users)
