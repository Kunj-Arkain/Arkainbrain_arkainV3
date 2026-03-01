"""
ARKAINBRAIN — ACP Admin Routes

All 6 pages + JSON APIs for the Agent Control Plane.
"""

import json
import math
from datetime import datetime
from flask import request, jsonify, session, redirect

from admin import admin_bp
from admin.decorators import admin_required, role_required, stepup_required, audit_log

_esc = lambda s: str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;")


def _get_acp():
    from config.database import get_db
    from config.acp_engine import ACP
    db = get_db()
    acp = ACP(db)
    return acp, db


# ═══════════════════════════════════════════════════════════
# Admin Layout Shell
# ═══════════════════════════════════════════════════════════

ACP_CSS = """
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#0a0b10;--surface:#12131a;--card:#1a1b25;--border:#252636;--accent:#7c6aef;
--success:#22c55e;--danger:#ef4444;--warn:#f59e0b;--info:#06b6d4;--text:#e2e8f0;
--dim:#94a3b8;--muted:#64748b;--radius:8px;--mono:'JetBrains Mono',monospace}
body{font-family:'Inter',-apple-system,sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
.acp-shell{display:flex;min-height:100vh}
.acp-side{width:220px;background:var(--surface);border-right:1px solid var(--border);padding:16px 0;position:fixed;top:0;bottom:0;overflow-y:auto;z-index:10}
.acp-side .logo{padding:12px 20px;font-size:15px;font-weight:800;color:var(--accent);border-bottom:1px solid var(--border);margin-bottom:8px}
.acp-side .logo span{font-size:9px;color:var(--dim);display:block;margin-top:2px;letter-spacing:1px}
.acp-side a{display:flex;align-items:center;gap:10px;padding:10px 20px;font-size:12px;color:var(--dim);text-decoration:none;transition:all .15s}
.acp-side a:hover{background:rgba(124,106,239,.08);color:var(--text)}
.acp-side a.active{background:rgba(124,106,239,.15);color:var(--accent);font-weight:600;border-right:2px solid var(--accent)}
.acp-side .back{margin-top:auto;padding-top:16px;border-top:1px solid var(--border)}
.acp-main{margin-left:220px;flex:1;padding:24px 32px;max-width:1300px}
.page-title{font-size:22px;font-weight:800;margin-bottom:4px}
.page-sub{font-size:12px;color:var(--dim);margin-bottom:24px}
.card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:16px;margin-bottom:14px}
.card h3{font-size:13px;font-weight:700;margin-bottom:10px}
.stat-row{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px;margin-bottom:16px}
.stat-box{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:14px;text-align:center}
.stat-box .val{font-size:28px;font-weight:800;color:var(--accent)}
.stat-box .lbl{font-size:10px;color:var(--dim);margin-top:2px;text-transform:uppercase;letter-spacing:.5px}
.stat-box.warn .val{color:var(--warn)}.stat-box.danger .val{color:var(--danger)}.stat-box.ok .val{color:var(--success)}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;padding:8px 10px;color:var(--dim);font-weight:600;font-size:10px;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid var(--border)}
td{padding:8px 10px;border-bottom:1px solid var(--border)}
tr:hover td{background:rgba(124,106,239,.04)}
.badge{display:inline-block;font-size:9px;padding:2px 8px;border-radius:10px;font-weight:700}
.badge-active{background:rgba(34,197,94,.2);color:var(--success)}
.badge-inactive{background:rgba(148,163,184,.15);color:var(--dim)}
.badge-danger{background:rgba(239,68,68,.2);color:var(--danger)}
.badge-warn{background:rgba(245,158,11,.2);color:var(--warn)}
.badge-accent{background:rgba(124,106,239,.2);color:var(--accent)}
.btn{display:inline-block;padding:6px 14px;font-size:11px;font-weight:600;border:none;border-radius:6px;cursor:pointer;text-decoration:none;transition:all .15s}
.btn-primary{background:var(--accent);color:#fff}.btn-primary:hover{filter:brightness(1.15)}
.btn-success{background:var(--success);color:#fff}.btn-danger{background:var(--danger);color:#fff}
.btn-outline{background:transparent;border:1px solid var(--border);color:var(--dim)}.btn-outline:hover{border-color:var(--accent);color:var(--accent)}
.btn-sm{padding:4px 10px;font-size:10px}
.toggle{position:relative;display:inline-block;width:36px;height:20px;cursor:pointer}
.toggle input{opacity:0;width:0;height:0}
.toggle .slider{position:absolute;inset:0;background:var(--border);border-radius:20px;transition:.2s}
.toggle input:checked + .slider{background:var(--success)}
.toggle .slider:before{content:"";position:absolute;height:14px;width:14px;left:3px;bottom:3px;background:#fff;border-radius:50%;transition:.2s}
.toggle input:checked + .slider:before{transform:translateX(16px)}
input[type=text],input[type=number],select,textarea{background:var(--surface);border:1px solid var(--border);color:var(--text);padding:8px 12px;border-radius:6px;font-size:12px;width:100%}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent)}
.form-row{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
.form-field{margin-bottom:12px}
.form-field label{display:block;font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px;font-weight:600}
.tab-bar{display:flex;gap:0;margin-bottom:20px;border-bottom:1px solid var(--border)}
.tab-bar a{padding:10px 18px;font-size:12px;color:var(--dim);text-decoration:none;border-bottom:2px solid transparent;transition:all .15s}
.tab-bar a:hover{color:var(--text)}.tab-bar a.active{color:var(--accent);border-bottom-color:var(--accent);font-weight:600}
.json-view{background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:12px;font-family:var(--mono);font-size:11px;white-space:pre-wrap;max-height:400px;overflow-y:auto;color:var(--dim)}
.diff-add{color:var(--success);background:rgba(34,197,94,.1)}.diff-rm{color:var(--danger);background:rgba(239,68,68,.1);text-decoration:line-through}
.alert{padding:10px 16px;border-radius:6px;font-size:12px;margin-bottom:12px}
.alert-warn{background:rgba(245,158,11,.1);border:1px solid rgba(245,158,11,.3);color:var(--warn)}
.alert-ok{background:rgba(34,197,94,.1);border:1px solid rgba(34,197,94,.3);color:var(--success)}
.mono{font-family:var(--mono);font-size:11px}
</style>
"""

NAV_ITEMS = [
    ("dashboard", "📊 Dashboard", "/admin/"),
    ("profiles",  "🎛️ Profiles",  "/admin/profiles"),
    ("flags",     "🚩 Flags",     "/admin/flags"),
    ("agents",    "🤖 Agents",    "/admin/agents"),
    ("workflows", "🔗 Workflows", "/admin/workflows"),
    ("capacity",  "⚡ Capacity",  "/admin/capacity"),
    ("audit",     "📋 Audit",     "/admin/audit"),
    # ── Site Administration ──
    ("users",     "👥 Users",     "/admin/users"),
    ("jobs",      "🔄 Jobs",      "/admin/jobs"),
    ("games",     "🎮 Games",     "/admin/games"),
    ("system",    "🖥 System",    "/admin/system"),
    ("settings",  "⚙️ Settings",  "/admin/settings"),
]


def acp_layout(content: str, active: str = "dashboard"):
    nav = ""
    for key, label, href in NAV_ITEMS:
        if key == "users":
            nav += '<div style="margin:12px 20px 6px;font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;border-top:1px solid var(--border);padding-top:10px">Site Admin</div>\n'
        cls = " active" if key == active else ""
        nav += f'<a href="{href}" class="{cls}">{label}</a>\n'
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ArkainBrain — Agent Control Plane</title>{ACP_CSS}
</head><body>
<div class="acp-shell">
    <nav class="acp-side">
        <div class="logo">⚡ Agent Control Plane<span>ARKAINBRAIN ACP</span></div>
        {nav}
        <div class="back"><a href="/">← Back to App</a></div>
    </nav>
    <main class="acp-main">{content}</main>
</div>
</body></html>"""


# ═══════════════════════════════════════════════════════════
# Page 1: Dashboard
# ═══════════════════════════════════════════════════════════

@admin_bp.route("/")
@admin_required
def acp_dashboard():
    acp, db = _get_acp()
    acp.seed_defaults()  # Idempotent
    s = acp.get_dashboard_stats()

    queue_cls = "danger" if s["queue_paused"] else "ok"
    estop_cls = "danger" if s["emergency_stop"] else "ok"

    changes_html = ""
    for ch in s.get("recent_changes", [])[:5]:
        changes_html += (
            f'<tr><td class="mono">{_esc(ch.get("scope",""))}/{_esc(ch.get("target_id","")[:15])}</td>'
            f'<td>v{ch.get("version_num","")}</td>'
            f'<td>{_esc(ch.get("change_reason","")[:60])}</td>'
            f'<td class="mono">{_esc(ch.get("created_by",""))}</td>'
            f'<td>{_esc(ch.get("created_at","")[:19])}</td></tr>'
        )

    return acp_layout(f"""
    <h1 class="page-title">Agent Control Plane</h1>
    <p class="page-sub">Runtime configuration for all AI agents, workflows, and budgets</p>

    <div class="stat-row">
        <div class="stat-box"><div class="val">{s["active_jobs"]}</div><div class="lbl">Active Jobs</div></div>
        <div class="stat-box"><div class="val">{s["total_jobs_today"]}</div><div class="lbl">Jobs Today</div></div>
        <div class="stat-box"><div class="val">{s["enabled_agents"]}/{s["total_agents"]}</div><div class="lbl">Agents Active</div></div>
        <div class="stat-box"><div class="val">{_esc(s["active_profile"])}</div><div class="lbl">Active Profile</div></div>
        <div class="stat-box {queue_cls}"><div class="val">{"⏸️ PAUSED" if s["queue_paused"] else "▶️ Running"}</div><div class="lbl">Queue</div></div>
        <div class="stat-box {estop_cls}"><div class="val">{"🛑 STOP" if s["emergency_stop"] else "✅ OK"}</div><div class="lbl">Emergency</div></div>
    </div>

    <div class="card">
        <h3>Quick Actions</h3>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
            <form method="POST" action="/admin/api/toggle-queue" style="display:inline">
                <button class="btn {"btn-success" if s["queue_paused"] else "btn-outline"}" type="submit">
                    {"▶️ Resume Queue" if s["queue_paused"] else "⏸️ Pause Queue"}
                </button>
            </form>
            <a href="/admin/profiles" class="btn btn-primary">🎛️ Switch Profile</a>
            <a href="/admin/flags" class="btn btn-outline">🚩 Feature Flags</a>
            <a href="/admin/agents" class="btn btn-outline">🤖 Agent Roster</a>
        </div>
    </div>

    <div class="card">
        <h3>Recent Config Changes</h3>
        <table>
            <tr><th>Scope/Target</th><th>Version</th><th>Reason</th><th>By</th><th>When</th></tr>
            {changes_html or '<tr><td colspan="5" style="color:var(--dim)">No changes yet</td></tr>'}
        </table>
    </div>
    """, "dashboard")


# ═══════════════════════════════════════════════════════════
# Page 2: Profiles
# ═══════════════════════════════════════════════════════════

@admin_bp.route("/profiles")
@admin_required
def acp_profiles():
    acp, db = _get_acp()
    profiles = acp.get_profiles()

    rows = ""
    for p in profiles:
        active = '<span class="badge badge-active">ACTIVE</span>' if p["is_active"] else '<span class="badge badge-inactive">inactive</span>'
        pj = json.loads(p["profile_json"]) if isinstance(p["profile_json"], str) else p["profile_json"]
        model = pj.get("model_heavy", "—")
        cost = pj.get("max_cost_per_job", "—")
        rows += f"""<tr>
            <td><strong>{_esc(p["name"])}</strong></td>
            <td>{active}</td>
            <td class="mono">{_esc(model)}</td>
            <td>${cost}</td>
            <td>{_esc(p.get("description",""))[:60]}</td>
            <td>
                <a href="/admin/profiles/{p["id"]}" class="btn btn-sm btn-outline">Edit</a>
                {"" if p["is_active"] else f'<form method="POST" action="/admin/api/profiles/{p["id"]}/activate" style="display:inline"><button class="btn btn-sm btn-success" type="submit">Activate</button></form>'}
                <form method="POST" action="/admin/api/profiles/{p["id"]}/clone" style="display:inline"><button class="btn btn-sm btn-outline" type="submit">Clone</button></form>
            </td></tr>"""

    return acp_layout(f"""
    <h1 class="page-title">Agent Profiles</h1>
    <p class="page-sub">Named runtime presets — budgets, models, and knobs</p>

    <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
            <h3>All Profiles</h3>
            <a href="/admin/profiles/new" class="btn btn-primary">+ New Profile</a>
        </div>
        <table>
            <tr><th>Name</th><th>Status</th><th>Model</th><th>Max Cost</th><th>Description</th><th>Actions</th></tr>
            {rows}
        </table>
    </div>
    """, "profiles")


@admin_bp.route("/profiles/new", methods=["GET", "POST"])
@admin_required
def acp_profile_new():
    if request.method == "POST":
        acp, db = _get_acp()
        name = request.form.get("name", "unnamed")
        config = {
            "model_heavy": request.form.get("model_heavy", "openai/gpt-4.1"),
            "model_light": request.form.get("model_light", "openai/gpt-4.1-mini"),
            "model_vision": request.form.get("model_vision", "gpt-4.1"),
            "max_tokens": int(request.form.get("max_tokens", 128000)),
            "max_iterations": int(request.form.get("max_iterations", 25)),
            "max_cost_per_job": float(request.form.get("max_cost_per_job", 25)),
            "convergence_loops": int(request.form.get("convergence_loops", 3)),
        }
        desc = request.form.get("description", "")
        user = session.get("user", {})
        acp.create_profile(name, config, user.get("id", "admin"), desc)
        audit_log("create_profile", "profile", name)
        return redirect("/admin/profiles")

    return acp_layout(_profile_form("New Profile", {}, "Create"), "profiles")


@admin_bp.route("/profiles/<pid>", methods=["GET", "POST"])
@admin_required
def acp_profile_edit(pid):
    acp, db = _get_acp()
    profile = acp.get_profile(pid)
    if not profile:
        return redirect("/admin/profiles")

    if request.method == "POST":
        config = {
            "model_heavy": request.form.get("model_heavy", "openai/gpt-4.1"),
            "model_light": request.form.get("model_light", "openai/gpt-4.1-mini"),
            "model_vision": request.form.get("model_vision", "gpt-4.1"),
            "max_tokens": int(request.form.get("max_tokens", 128000)),
            "max_iterations": int(request.form.get("max_iterations", 25)),
            "max_cost_per_job": float(request.form.get("max_cost_per_job", 25)),
            "convergence_loops": int(request.form.get("convergence_loops", 3)),
        }
        reason = request.form.get("change_reason", "")
        user = session.get("user", {})
        acp.update_profile(pid, config, user.get("id", "admin"), reason)
        audit_log("update_profile", "profile", pid, {"reason": reason})
        return redirect("/admin/profiles")

    pj = json.loads(profile["profile_json"]) if isinstance(profile["profile_json"], str) else profile["profile_json"]

    # Version history
    versions = acp.get_versions("profile", pid, limit=10)
    ver_html = ""
    for v in versions:
        ver_html += (f'<tr><td>v{v["version_num"]}</td>'
                     f'<td>{_esc(v.get("change_reason","")[:50])}</td>'
                     f'<td>{_esc(v.get("created_by",""))}</td>'
                     f'<td>{_esc(v.get("created_at","")[:19])}</td>'
                     f'<td><form method="POST" action="/admin/api/rollback" style="display:inline">'
                     f'<input type="hidden" name="scope" value="profile"><input type="hidden" name="target_id" value="{pid}">'
                     f'<input type="hidden" name="version" value="{v["version_num"]}">'
                     f'<input type="hidden" name="confirm_stepup" value="1">'
                     f'<button class="btn btn-sm btn-outline" type="submit">Rollback</button></form></td></tr>')

    return acp_layout(f"""
    <h1 class="page-title">Edit: {_esc(profile["name"])}</h1>
    <p class="page-sub">Profile ID: {pid}</p>
    {_profile_form("Update Profile", pj, "Save Changes", profile.get("name",""), profile.get("description",""))}

    <div class="card" style="margin-top:20px">
        <h3>Version History</h3>
        <table><tr><th>Version</th><th>Reason</th><th>By</th><th>When</th><th>Action</th></tr>
        {ver_html or '<tr><td colspan="5" style="color:var(--dim)">No versions yet</td></tr>'}
        </table>
    </div>

    <div class="card"><h3>Raw JSON</h3>
    <div class="json-view">{_esc(json.dumps(pj, indent=2))}</div></div>
    """, "profiles")


def _profile_form(title, config, btn_text, name="", desc=""):
    models = ["openai/gpt-4.1", "openai/gpt-4.1-mini", "openai/gpt-4.1-nano",
              "openai/gpt-5", "openai/gpt-5-mini", "openai/gpt-5.2",
              "openai/gpt-4o", "openai/gpt-4o-mini"]

    def sel(field, val):
        return "".join(f'<option value="{m}" {"selected" if m==val else ""}>{m}</option>' for m in models)

    return f"""<div class="card"><h3>{title}</h3>
    <form method="POST">
        <div class="form-row">
            <div class="form-field"><label>Profile Name</label>
                <input type="text" name="name" value="{_esc(name)}" required></div>
            <div class="form-field"><label>Description</label>
                <input type="text" name="description" value="{_esc(desc)}"></div>
        </div>
        <div class="form-row">
            <div class="form-field"><label>Model (Heavy/Creative)</label>
                <select name="model_heavy">{sel("model_heavy", config.get("model_heavy","openai/gpt-4.1"))}</select></div>
            <div class="form-field"><label>Model (Light/Validation)</label>
                <select name="model_light">{sel("model_light", config.get("model_light","openai/gpt-4.1-mini"))}</select></div>
        </div>
        <div class="form-row">
            <div class="form-field"><label>Model (Vision QA)</label>
                <select name="model_vision">{sel("model_vision", config.get("model_vision","gpt-4.1"))}</select></div>
            <div class="form-field"><label>Max Tokens per Agent</label>
                <input type="number" name="max_tokens" value="{config.get("max_tokens",128000)}"></div>
        </div>
        <div class="form-row">
            <div class="form-field"><label>Max Iterations per Agent</label>
                <input type="number" name="max_iterations" value="{config.get("max_iterations",25)}"></div>
            <div class="form-field"><label>Max Cost per Job ($)</label>
                <input type="number" name="max_cost_per_job" value="{config.get("max_cost_per_job",25)}" step="0.5"></div>
        </div>
        <div class="form-row">
            <div class="form-field"><label>Convergence Loops</label>
                <input type="number" name="convergence_loops" value="{config.get("convergence_loops",3)}" min="0" max="10"></div>
            <div class="form-field"><label>Change Reason</label>
                <input type="text" name="change_reason" placeholder="Why this change?"></div>
        </div>
        <button type="submit" class="btn btn-primary">{btn_text}</button>
    </form></div>"""


# ═══════════════════════════════════════════════════════════
# Page 3: Feature Flags
# ═══════════════════════════════════════════════════════════

@admin_bp.route("/flags")
@admin_required
def acp_flags():
    acp, db = _get_acp()
    settings = acp.get_all_settings()
    cat_filter = request.args.get("cat", "")

    categories = sorted(set(s.get("category", "general") for s in settings))
    tab_html = '<a href="/admin/flags" class="' + ("active" if not cat_filter else "") + '">All</a>'
    for c in categories:
        tab_html += f'<a href="/admin/flags?cat={c}" class="{"active" if c==cat_filter else ""}">{c.title()}</a>'

    rows = ""
    for s in settings:
        if cat_filter and s.get("category") != cat_filter:
            continue
        key = s["key"]
        val = json.loads(s["value_json"]) if isinstance(s.get("value_json"), str) else s.get("value_json")
        stype = s.get("type", "string")
        stepup = "🔐" if s.get("requires_stepup") else ""

        if stype == "bool":
            checked = "checked" if val else ""
            input_html = f"""<label class="toggle">
                <input type="checkbox" {checked} onchange="toggleFlag('{key}', this.checked)">
                <span class="slider"></span></label>"""
        elif stype in ("int", "float"):
            step = "1" if stype == "int" else "0.1"
            input_html = f'<input type="number" value="{val}" step="{step}" style="width:100px" onchange="setFlag(\'{key}\', this.value, \'{stype}\')">'
        else:
            input_html = f'<input type="text" value="{_esc(str(val))}" style="width:200px" onchange="setFlag(\'{key}\', this.value, \'string\')">'

        cat_badge = f'<span class="badge badge-{"active" if s.get("category")=="feature" else "inactive"}">{s.get("category","")}</span>'
        rows += f"""<tr>
            <td class="mono">{_esc(key)}</td>
            <td>{cat_badge}</td>
            <td>{input_html}</td>
            <td style="font-size:11px;color:var(--dim)">{_esc(s.get("description","")[:60])}</td>
            <td>{stepup}</td>
            <td class="mono" style="font-size:10px;color:var(--muted)">{_esc(s.get("updated_by","")[:10])}</td></tr>"""

    return acp_layout(f"""
    <h1 class="page-title">Feature Flags & Settings</h1>
    <p class="page-sub">Toggle features, set limits, and configure routing — changes apply to new jobs</p>
    <div class="tab-bar">{tab_html}</div>
    <div class="card">
        <table>
            <tr><th>Key</th><th>Category</th><th>Value</th><th>Description</th><th>🔐</th><th>Last By</th></tr>
            {rows}
        </table>
    </div>
    <script>
    async function toggleFlag(key, val) {{
        await fetch('/admin/api/flags/' + key, {{
            method: 'POST', headers: {{'Content-Type':'application/json'}},
            body: JSON.stringify({{value: val, type: 'bool'}})
        }});
    }}
    async function setFlag(key, val, type) {{
        if (type === 'int') val = parseInt(val);
        else if (type === 'float') val = parseFloat(val);
        await fetch('/admin/api/flags/' + key, {{
            method: 'POST', headers: {{'Content-Type':'application/json'}},
            body: JSON.stringify({{value: val, type: type}})
        }});
    }}
    </script>
    """, "flags")


# ═══════════════════════════════════════════════════════════
# Page 4: Agent Roster
# ═══════════════════════════════════════════════════════════

@admin_bp.route("/agents")
@admin_required
def acp_agents():
    acp, db = _get_acp()
    agents = acp.get_agents()

    rows = ""
    for a in agents:
        enabled = '<span class="badge badge-active">ON</span>' if a["is_enabled"] else '<span class="badge badge-danger">OFF</span>'
        toggle_val = 0 if a["is_enabled"] else 1
        toggle_label = "Disable" if a["is_enabled"] else "Enable"
        toggle_cls = "btn-outline" if a["is_enabled"] else "btn-success"
        rows += f"""<tr>
            <td><strong>{_esc(a["name"])}</strong><br><span style="font-size:10px;color:var(--dim)">{_esc(a.get("role","")[:50])}</span></td>
            <td>{enabled}</td>
            <td class="mono" style="font-size:11px">{_esc(a.get("model_override","") or "profile default")}</td>
            <td>{a.get("temperature",0.5)}</td>
            <td>{a.get("max_tokens",128000):,}</td>
            <td>{a.get("max_iterations",25)}</td>
            <td>
                <form method="POST" action="/admin/api/agents/{a["id"]}/toggle" style="display:inline">
                    <input type="hidden" name="enabled" value="{toggle_val}">
                    <button class="btn btn-sm {toggle_cls}" type="submit">{toggle_label}</button>
                </form>
                <a href="/admin/agents/{a["id"]}" class="btn btn-sm btn-outline">Edit</a>
            </td></tr>"""

    return acp_layout(f"""
    <h1 class="page-title">Agent Roster</h1>
    <p class="page-sub">Enable/disable agents, set models, temperatures, and tool permissions</p>
    <div class="card">
        <table>
            <tr><th>Agent</th><th>Status</th><th>Model</th><th>Temp</th><th>Max Tokens</th><th>Iterations</th><th>Actions</th></tr>
            {rows}
        </table>
    </div>
    """, "agents")


@admin_bp.route("/agents/<aid>", methods=["GET", "POST"])
@admin_required
def acp_agent_edit(aid):
    acp, db = _get_acp()
    agent = acp.get_agent(aid)
    if not agent:
        return redirect("/admin/agents")

    if request.method == "POST":
        updates = {
            "model_override": request.form.get("model_override", ""),
            "temperature": float(request.form.get("temperature", 0.5)),
            "max_tokens": int(request.form.get("max_tokens", 128000)),
            "max_iterations": int(request.form.get("max_iterations", 25)),
            "role": request.form.get("role", ""),
        }
        user = session.get("user", {})
        acp.update_agent(aid, updates, user.get("id", "admin"))
        audit_log("update_agent", "agent", aid, updates)
        return redirect("/admin/agents")

    models = ["", "openai/gpt-4.1", "openai/gpt-4.1-mini", "openai/gpt-4.1-nano",
              "openai/gpt-5", "openai/gpt-5-mini", "openai/gpt-4o"]
    model_sel = "".join(f'<option value="{m}" {"selected" if m==agent.get("model_override","") else ""}>{m or "(profile default)"}</option>' for m in models)

    return acp_layout(f"""
    <h1 class="page-title">Edit Agent: {_esc(agent["name"])}</h1>
    <p class="page-sub">ID: {aid}</p>
    <div class="card"><form method="POST">
        <div class="form-field"><label>Role / Backstory</label>
            <input type="text" name="role" value="{_esc(agent.get("role",""))}"></div>
        <div class="form-row">
            <div class="form-field"><label>Model Override (empty = use profile)</label>
                <select name="model_override">{model_sel}</select></div>
            <div class="form-field"><label>Temperature</label>
                <input type="number" name="temperature" value="{agent.get("temperature",0.5)}" step="0.05" min="0" max="2"></div>
        </div>
        <div class="form-row">
            <div class="form-field"><label>Max Tokens</label>
                <input type="number" name="max_tokens" value="{agent.get("max_tokens",128000)}"></div>
            <div class="form-field"><label>Max Iterations</label>
                <input type="number" name="max_iterations" value="{agent.get("max_iterations",25)}"></div>
        </div>
        <button type="submit" class="btn btn-primary">Save Changes</button>
        <a href="/admin/agents" class="btn btn-outline" style="margin-left:8px">Cancel</a>
    </form></div>
    """, "agents")


# ═══════════════════════════════════════════════════════════
# Page 5: Workflows
# ═══════════════════════════════════════════════════════════

@admin_bp.route("/workflows")
@admin_required
def acp_workflows():
    acp, db = _get_acp()
    workflows = acp.get_workflows()
    agents = {a["id"]: a["name"] for a in acp.get_agents()}

    rows = ""
    for w in workflows:
        active = '<span class="badge badge-active">ACTIVE</span>' if w["is_active"] else '<span class="badge badge-inactive">inactive</span>'
        seq = json.loads(w["agent_sequence_json"]) if isinstance(w["agent_sequence_json"], str) else w["agent_sequence_json"]
        agent_names = [s.get("agent_id", "?") for s in seq[:6]]
        chain = " → ".join(agent_names) + ("…" if len(seq) > 6 else "")
        rows += f"""<tr>
            <td><strong>{_esc(w["name"])}</strong></td>
            <td class="mono">{_esc(w["job_type"])}</td>
            <td>{active}</td>
            <td>{len(seq)} steps</td>
            <td style="font-size:10px;color:var(--dim)">{_esc(chain)}</td>
            <td>
                {"" if w["is_active"] else f'<form method="POST" action="/admin/api/workflows/{w["id"]}/activate" style="display:inline"><button class="btn btn-sm btn-success" type="submit">Activate</button></form>'}
                <a href="/admin/workflows/{w["id"]}" class="btn btn-sm btn-outline">Edit</a>
            </td></tr>"""

    return acp_layout(f"""
    <h1 class="page-title">Workflow Templates</h1>
    <p class="page-sub">Ordered agent graphs per job type — reorder, override, activate</p>
    <div class="card">
        <table>
            <tr><th>Name</th><th>Job Type</th><th>Status</th><th>Steps</th><th>Agent Chain</th><th>Actions</th></tr>
            {rows}
        </table>
    </div>
    """, "workflows")


@admin_bp.route("/workflows/<wid>")
@admin_required
def acp_workflow_detail(wid):
    acp, db = _get_acp()
    wf = None
    for w in acp.get_workflows():
        if w["id"] == wid:
            wf = w
            break
    if not wf:
        return redirect("/admin/workflows")

    seq = json.loads(wf["agent_sequence_json"]) if isinstance(wf["agent_sequence_json"], str) else wf["agent_sequence_json"]
    steps_html = ""
    for i, step in enumerate(seq):
        overrides = {k: v for k, v in step.items() if k not in ("agent_id", "stage")}
        ov_str = json.dumps(overrides) if overrides else "—"
        steps_html += f"""<tr>
            <td>{i+1}</td>
            <td class="mono"><strong>{_esc(step.get("agent_id",""))}</strong></td>
            <td>{_esc(step.get("stage",""))}</td>
            <td class="mono" style="font-size:10px;color:var(--dim)">{_esc(ov_str[:80])}</td></tr>"""

    return acp_layout(f"""
    <h1 class="page-title">{_esc(wf["name"])}</h1>
    <p class="page-sub">Job type: {_esc(wf["job_type"])} · ID: {wid}</p>
    <div class="card"><h3>Agent Sequence</h3>
        <table><tr><th>#</th><th>Agent</th><th>Stage</th><th>Overrides</th></tr>
        {steps_html}</table>
    </div>
    <div class="card"><h3>Raw JSON</h3>
    <div class="json-view">{_esc(json.dumps(seq, indent=2))}</div></div>
    """, "workflows")


# ═══════════════════════════════════════════════════════════
# Page 6: Capacity & Limits
# ═══════════════════════════════════════════════════════════

@admin_bp.route("/capacity")
@admin_required
def acp_capacity():
    acp, db = _get_acp()

    concurrent = acp.get_setting("max_concurrent_jobs", 3)
    paused = acp.flag("queue_paused")
    estop = acp.flag("emergency_stop")
    job_timeout = acp.get_setting("job_timeout_seconds", 3600)
    stage_timeout = acp.get_setting("stage_timeout_seconds", 600)
    max_cost_job = acp.get_setting("max_cost_per_job", 25.0)
    max_cost_day = acp.get_setting("max_cost_per_day", 200.0)
    max_cost_user = acp.get_setting("max_cost_per_user_day", 50.0)
    max_iters = acp.get_setting("max_agent_iterations", 25)

    queued = db.execute("SELECT COUNT(*) as c FROM jobs WHERE status='queued'").fetchone()["c"]
    running = db.execute("SELECT COUNT(*) as c FROM jobs WHERE status='running'").fetchone()["c"]

    return acp_layout(f"""
    <h1 class="page-title">Capacity & Limits</h1>
    <p class="page-sub">Concurrency, queue controls, timeouts, and cost guardrails</p>

    <div class="stat-row">
        <div class="stat-box"><div class="val">{running}</div><div class="lbl">Running Now</div></div>
        <div class="stat-box"><div class="val">{queued}</div><div class="lbl">In Queue</div></div>
        <div class="stat-box"><div class="val">{concurrent}</div><div class="lbl">Max Concurrent</div></div>
    </div>

    <div class="card"><h3>Queue Controls</h3>
    <div style="display:flex;gap:8px">
        <form method="POST" action="/admin/api/toggle-queue"><button class="btn {"btn-success" if paused else "btn-outline"}" type="submit">{"▶️ Resume" if paused else "⏸️ Pause"}</button></form>
        <form method="POST" action="/admin/api/emergency-stop"><input type="hidden" name="confirm_stepup" value="1"><button class="btn btn-danger" type="submit">🛑 Emergency Stop</button></form>
        {"<div class='alert alert-warn'>⚠️ EMERGENCY STOP is active — no new jobs will start</div>" if estop else ""}
    </div></div>

    <div class="card"><h3>Limits</h3>
    <form method="POST" action="/admin/api/capacity">
        <div class="form-row">
            <div class="form-field"><label>Max Concurrent Jobs</label>
                <input type="number" name="max_concurrent_jobs" value="{concurrent}" min="1" max="20"></div>
            <div class="form-field"><label>Max Agent Iterations</label>
                <input type="number" name="max_agent_iterations" value="{max_iters}" min="1" max="100"></div>
        </div>
        <div class="form-row">
            <div class="form-field"><label>Job Timeout (seconds)</label>
                <input type="number" name="job_timeout_seconds" value="{job_timeout}"></div>
            <div class="form-field"><label>Stage Timeout (seconds)</label>
                <input type="number" name="stage_timeout_seconds" value="{stage_timeout}"></div>
        </div>
        <div class="form-row">
            <div class="form-field"><label>Max Cost per Job ($) 🔐</label>
                <input type="number" name="max_cost_per_job" value="{max_cost_job}" step="0.5"></div>
            <div class="form-field"><label>Max Cost per Day ($) 🔐</label>
                <input type="number" name="max_cost_per_day" value="{max_cost_day}" step="1"></div>
        </div>
        <div class="form-row">
            <div class="form-field"><label>Max Cost per User/Day ($)</label>
                <input type="number" name="max_cost_per_user_day" value="{max_cost_user}" step="1"></div>
            <div class="form-field"></div>
        </div>
        <input type="hidden" name="confirm_stepup" value="1">
        <button type="submit" class="btn btn-primary">Save Limits</button>
    </form></div>
    """, "capacity")


# ═══════════════════════════════════════════════════════════
# Page 7: Audit & Rollback
# ═══════════════════════════════════════════════════════════

@admin_bp.route("/audit")
@admin_required
def acp_audit():
    acp, db = _get_acp()
    page = int(request.args.get("page", 1))
    per_page = 30
    scope_filter = request.args.get("scope", "")

    # Config versions (the real audit trail)
    sql = "SELECT * FROM config_versions WHERE 1=1"
    params = []
    if scope_filter:
        sql += " AND scope=?"
        params.append(scope_filter)
    sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([per_page, (page - 1) * per_page])

    rows = db.execute(sql, params).fetchall()
    versions = [dict(r) for r in rows] if rows else []

    # Scope tabs
    tab_html = f'<a href="/admin/audit" class="{"active" if not scope_filter else ""}">All</a>'
    for s in ["system", "profile", "workflow", "agent"]:
        tab_html += f'<a href="/admin/audit?scope={s}" class="{"active" if s==scope_filter else ""}">{s.title()}</a>'

    ver_rows = ""
    for v in versions:
        ver_rows += f"""<tr>
            <td class="mono">{_esc(v.get("scope",""))}</td>
            <td class="mono">{_esc(v.get("target_id","")[:20])}</td>
            <td>v{v.get("version_num","")}</td>
            <td>{_esc(v.get("change_reason","")[:60])}</td>
            <td>{_esc(v.get("created_by",""))}</td>
            <td>{_esc(v.get("created_at","")[:19])}</td>
            <td>
                <form method="POST" action="/admin/api/rollback" style="display:inline">
                    <input type="hidden" name="scope" value="{v.get("scope","")}">
                    <input type="hidden" name="target_id" value="{v.get("target_id","")}">
                    <input type="hidden" name="version" value="{v.get("version_num","")}">
                    <input type="hidden" name="confirm_stepup" value="1">
                    <button class="btn btn-sm btn-outline" type="submit">↩️ Rollback</button>
                </form>
            </td></tr>"""

    # Admin audit log
    audit_rows_raw = db.execute(
        "SELECT * FROM admin_audit_log ORDER BY created_at DESC LIMIT 20"
    ).fetchall()
    audit_html = ""
    for a in (audit_rows_raw or []):
        a = dict(a)
        audit_html += f"""<tr>
            <td>{_esc(a.get("action",""))}</td>
            <td class="mono">{_esc(a.get("target_type",""))}/{_esc(a.get("target_id","")[:15])}</td>
            <td>{_esc(a.get("admin_id",""))}</td>
            <td>{_esc(a.get("created_at","")[:19])}</td></tr>"""

    return acp_layout(f"""
    <h1 class="page-title">Audit & Rollback</h1>
    <p class="page-sub">Every config change — versioned, diffable, rollbackable</p>

    <div class="tab-bar">{tab_html}</div>

    <div class="card"><h3>Config Versions</h3>
        <table>
            <tr><th>Scope</th><th>Target</th><th>Version</th><th>Reason</th><th>By</th><th>When</th><th>Action</th></tr>
            {ver_rows or '<tr><td colspan="7" style="color:var(--dim)">No versions yet</td></tr>'}
        </table>
        <div style="margin-top:12px;display:flex;gap:8px">
            {"<a href='/admin/audit?page="+str(page-1)+("&scope="+scope_filter if scope_filter else "")+"' class='btn btn-sm btn-outline'>← Prev</a>" if page > 1 else ""}
            <a href="/admin/audit?page={page+1}{"&scope="+scope_filter if scope_filter else ""}" class="btn btn-sm btn-outline">Next →</a>
        </div>
    </div>

    <div class="card"><h3>Admin Actions Log</h3>
        <table>
            <tr><th>Action</th><th>Target</th><th>Admin</th><th>When</th></tr>
            {audit_html or '<tr><td colspan="4" style="color:var(--dim)">No actions yet</td></tr>'}
        </table>
    </div>
    """, "audit")


# ═══════════════════════════════════════════════════════════
# API Endpoints
# ═══════════════════════════════════════════════════════════

@admin_bp.route("/api/flags/<key>", methods=["POST"])
@admin_required
def api_set_flag(key):
    acp, db = _get_acp()
    data = request.get_json(silent=True) or {}
    value = data.get("value")
    user = session.get("user", {})
    acp.set_setting(key, value, user.get("id", "admin"), f"Set via UI")
    audit_log("set_flag", "setting", key, {"value": value})
    return jsonify({"ok": True, "key": key, "value": value})


@admin_bp.route("/api/profiles/<pid>/activate", methods=["POST"])
@admin_required
def api_activate_profile(pid):
    acp, db = _get_acp()
    user = session.get("user", {})
    acp.activate_profile(pid, user.get("id", "admin"))
    audit_log("activate_profile", "profile", pid)
    return redirect("/admin/profiles")


@admin_bp.route("/api/profiles/<pid>/clone", methods=["POST"])
@admin_required
def api_clone_profile(pid):
    acp, db = _get_acp()
    user = session.get("user", {})
    profile = acp.get_profile(pid)
    if profile:
        new_name = profile["name"] + " (copy)"
        acp.clone_profile(pid, new_name, user.get("id", "admin"))
        audit_log("clone_profile", "profile", pid)
    return redirect("/admin/profiles")


@admin_bp.route("/api/agents/<aid>/toggle", methods=["POST"])
@admin_required
def api_toggle_agent(aid):
    acp, db = _get_acp()
    enabled = request.form.get("enabled", "1") == "1"
    user = session.get("user", {})
    acp.toggle_agent(aid, enabled, user.get("id", "admin"))
    audit_log("toggle_agent", "agent", aid, {"enabled": enabled})
    return redirect("/admin/agents")


@admin_bp.route("/api/workflows/<wid>/activate", methods=["POST"])
@admin_required
def api_activate_workflow(wid):
    acp, db = _get_acp()
    user = session.get("user", {})
    acp.activate_workflow(wid, user.get("id", "admin"))
    audit_log("activate_workflow", "workflow", wid)
    return redirect("/admin/workflows")


@admin_bp.route("/api/toggle-queue", methods=["POST"])
@admin_required
def api_toggle_queue():
    acp, db = _get_acp()
    current = acp.flag("queue_paused")
    user = session.get("user", {})
    acp.set_setting("queue_paused", not current, user.get("id", "admin"),
                     "Toggled from dashboard")
    audit_log("toggle_queue", "system", "queue_paused", {"paused": not current})
    return redirect(request.referrer or "/admin/")


@admin_bp.route("/api/emergency-stop", methods=["POST"])
@admin_required
def api_emergency_stop():
    acp, db = _get_acp()
    user = session.get("user", {})
    acp.set_setting("emergency_stop", True, user.get("id", "admin"), "EMERGENCY STOP activated")
    audit_log("emergency_stop", "system", "emergency_stop", {"activated": True})
    return redirect("/admin/capacity")


@admin_bp.route("/api/capacity", methods=["POST"])
@admin_required
def api_set_capacity():
    acp, db = _get_acp()
    user = session.get("user", {})
    fields = {
        "max_concurrent_jobs": ("int", 3),
        "max_agent_iterations": ("int", 25),
        "job_timeout_seconds": ("int", 3600),
        "stage_timeout_seconds": ("int", 600),
        "max_cost_per_job": ("float", 25.0),
        "max_cost_per_day": ("float", 200.0),
        "max_cost_per_user_day": ("float", 50.0),
    }
    changed = []
    for key, (vtype, default) in fields.items():
        raw = request.form.get(key)
        if raw is not None:
            val = int(raw) if vtype == "int" else float(raw)
            acp.set_setting(key, val, user.get("id", "admin"), "Updated from capacity page")
            changed.append(key)
    audit_log("update_capacity", "system", "capacity", {"changed": changed})
    return redirect("/admin/capacity")


@admin_bp.route("/api/rollback", methods=["POST"])
@admin_required
@stepup_required
def api_rollback():
    acp, db = _get_acp()
    scope = request.form.get("scope")
    target_id = request.form.get("target_id")
    version = int(request.form.get("version", 0))
    user = session.get("user", {})

    try:
        acp.rollback(scope, target_id, version, user.get("id", "admin"))
        audit_log("rollback", scope, target_id, {"to_version": version})
    except Exception as e:
        return f"Rollback failed: {e}", 400

    return redirect(request.referrer or "/admin/audit")


# ── JSON API for dashboard data ──

@admin_bp.route("/api/stats")
@admin_required
def api_acp_stats():
    acp, db = _get_acp()
    return jsonify(acp.get_dashboard_stats())


@admin_bp.route("/api/resolve/<job_type>")
@admin_required
def api_resolve_config(job_type):
    acp, db = _get_acp()
    config = acp.resolve_config(job_type)
    return jsonify(config)
