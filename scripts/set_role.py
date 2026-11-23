"""List users or change a role - an ops tool, not an API endpoint.

Why not `PATCH /admin/users/{id}`? That endpoint is a privilege-escalation
surface: one bug in its authorization turns any account into an admin. Role
changes are rare and belong to whoever can already run commands against the
database. When there is a real user-management product need, the endpoint gets
built with an audit trail; until then this script is the smaller attack surface.

Usage (repo root):
    .\\.venv\\Scripts\\python.exe scripts\\set_role.py --list
    .\\.venv\\Scripts\\python.exe scripts\\set_role.py --email admin@test.com --role admin
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models.user import User  # noqa: E402

ROLES = ("agent", "tier2", "admin")


async def main(email: str | None, role: str | None, list_only: bool) -> int:
    async with SessionLocal() as session:
        if list_only or not email:
            users = (await session.execute(select(User).order_by(User.created_at))).scalars().all()
            if not users:
                print("no users")
                return 0
            for user in users:
                print(f"{user.email:32s} {user.role:6s} active={user.is_active}")
            return 0

        user = await session.scalar(select(User).where(User.email == email.lower()))
        if user is None:
            print(f"no user with email {email!r}", file=sys.stderr)
            return 1
        if role not in ROLES:
            print(f"role must be one of {', '.join(ROLES)}", file=sys.stderr)
            return 1
        before = user.role
        user.role = role
        await session.commit()
        print(f"{user.email}: {before} -> {role}")
        return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", help="user to change")
    parser.add_argument("--role", choices=ROLES, help="new role")
    parser.add_argument("--list", action="store_true", help="list users and exit")
    args = parser.parse_args()
    if bool(args.email) != bool(args.role) and not args.list:
        parser.error("--email and --role go together")
    raise SystemExit(asyncio.run(main(args.email, args.role, args.list)))
