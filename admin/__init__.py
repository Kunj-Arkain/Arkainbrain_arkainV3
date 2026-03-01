"""
ARKAINBRAIN — Agent Control Plane (ACP) Admin

Flask blueprint: /admin/*
12 pages: Dashboard, Profiles, Flags, Agents, Workflows, Capacity, Audit,
          Users, Jobs, Games, System, Settings
"""

from flask import Blueprint

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

from admin import routes  # noqa: E402, F401
from admin import site_routes  # noqa: E402, F401
