"""Authentication, sessions and role-based access control.

Built-in username/password (salted PBKDF2 via the standard library — no extra
deps) with opaque server-side sessions stored in the DB. This runs fully offline.

**SSO seam:** to front this with council single sign-on (Microsoft Entra / SAML /
OIDC), validate the IdP assertion in a new dependency and map the verified
identity to a `User` (provisioning on first login), then issue a session the same
way `login` does below. The capability model and audit attribution stay unchanged.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db
from .enums import Role
from .models import Session as SessionModel
from .models import User

settings = get_settings()


# --- Capabilities: what a role may do -----------------------------------------

class Cap:
    READ = "read"            # view cases, queue, inbox, knowledge base
    INTAKE = "intake"        # register a case, manage the mailbox (poll/import/link/dismiss)
    PROCESS = "process"      # triage, auto-draft, SME update, run compliance checks
    APPROVE = "approve"      # department manager approval
    SIGN_OFF = "sign_off"    # final Legal & IG sign-off
    DISPATCH = "dispatch"    # issue the response and close the case
    CONTRIBUTE = "contribute"  # add/upload documents to the knowledge base
    ADMIN = "admin"          # ingestion, reindex, user administration


_ALL = {Cap.READ, Cap.INTAKE, Cap.PROCESS, Cap.APPROVE, Cap.SIGN_OFF, Cap.DISPATCH,
        Cap.CONTRIBUTE, Cap.ADMIN}

# Separation of duties: approve / sign-off / dispatch are deliberately different roles.
ROLE_CAPS: dict[str, set[str]] = {
    Role.CASEWORKER.value: {Cap.READ, Cap.INTAKE, Cap.PROCESS},
    Role.MANAGER.value:    {Cap.READ, Cap.PROCESS, Cap.APPROVE},
    Role.LEGAL_IG.value:   {Cap.READ, Cap.SIGN_OFF},
    Role.FOI_TEAM.value:   {Cap.READ, Cap.INTAKE, Cap.DISPATCH},
    # Subject departments contribute the source material the drafter grounds on,
    # but do NO casework — CONTRIBUTE only, deliberately without READ, so they
    # cannot see FOI cases or requesters' personal data (data minimisation).
    Role.DEPARTMENT.value: {Cap.CONTRIBUTE},
    Role.ADMIN.value:      set(_ALL),
}


def role_caps(role: str) -> set[str]:
    return ROLE_CAPS.get(role, {Cap.READ})


# --- Password hashing (PBKDF2-HMAC-SHA256, stdlib) ----------------------------

_ALGO = "pbkdf2_sha256"
_ITERATIONS = 240_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _ITERATIONS)
    return f"{_ALGO}${_ITERATIONS}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _algo, iters, salt, expected = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iters))
        return hmac.compare_digest(dk.hex(), expected)
    except (ValueError, AttributeError):
        return False


def _utcnow() -> datetime:
    # Naive UTC, to compare cleanly with values read back from SQLite.
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --- User & session operations ------------------------------------------------

def create_user(db: Session, *, username: str, password: str, role: str,
                full_name: str = "", department: str = "") -> User:
    user = User(username=username, full_name=full_name or username,
                password_hash=hash_password(password), role=role,
                department=department or "")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def authenticate(db: Session, username: str, password: str) -> User | None:
    user = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
    if user and user.is_active and verify_password(password, user.password_hash):
        return user
    return None


def create_session(db: Session, user: User) -> str:
    token = secrets.token_urlsafe(32)
    db.add(SessionModel(token=token, user_id=user.id,
                        expires_at=_utcnow() + timedelta(hours=settings.session_ttl_hours)))
    db.commit()
    return token


def session_user(db: Session, token: str | None) -> User | None:
    if not token:
        return None
    s = db.get(SessionModel, token)
    if not s:
        return None
    if s.expires_at < _utcnow():
        db.delete(s)
        db.commit()
        return None
    user = db.get(User, s.user_id)
    return user if (user and user.is_active) else None


def delete_session(db: Session, token: str | None) -> None:
    if token and (s := db.get(SessionModel, token)):
        db.delete(s)
        db.commit()


# --- FastAPI dependencies -----------------------------------------------------

def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = session_user(db, request.cookies.get(settings.session_cookie_name))
    if not user and settings.demo_mode:
        # No-login demo: treat anonymous visitors as the configured demo user.
        user = db.execute(
            select(User).where(User.username == settings.demo_username)
        ).scalar_one_or_none()
    if not user:
        raise HTTPException(401, "Not authenticated")
    return user


def require(*caps: str):
    """Dependency factory: require the signed-in user's role to hold all *caps*."""
    needed = set(caps)

    def dependency(user: User = Depends(current_user)) -> User:
        if not needed.issubset(role_caps(user.role)):
            raise HTTPException(403, f"Your role ('{user.role}') is not permitted to perform this action.")
        return user

    return dependency


def require_live(*caps: str):
    """Like :func:`require`, but also refuses while the no-login public demo is
    on (FOI_DEMO_MODE). Used to gate destructive / irreversible / outbound-email
    actions so an anonymous demo visitor can browse and run the workflow, but
    cannot manage accounts, dispatch responses, edit the knowledge base, run
    crawls or send mail."""
    cap_gate = require(*caps)

    def dependency(user: User = Depends(cap_gate)) -> User:
        if settings.demo_mode:
            raise HTTPException(403, "This action is disabled in the public demo.")
        return user

    return dependency


# --- Default starter accounts (dev only — change in production) ---------------

# username, password, role, full name, department
DEFAULT_USERS = [
    ("caseworker", "caseworker", Role.CASEWORKER.value, "Sam Caseworker", ""),
    ("manager",    "manager",    Role.MANAGER.value,    "Morgan Manager", ""),
    ("legal",      "legal",      Role.LEGAL_IG.value,   "Lee (Legal & IG)", ""),
    ("foi",        "foi",        Role.FOI_TEAM.value,   "Frankie (FOI team)", ""),
    # Example subject-department account; admins create one per real department.
    ("highways",   "highways",   Role.DEPARTMENT.value, "Highways (Department)", "Highways"),
    ("admin",      "admin",      Role.ADMIN.value,      "Administrator", ""),
]


def ensure_seed_users(db: Session) -> int:
    """Create the starter accounts on first run (only if there are no users)."""
    if not settings.seed_default_users:
        return 0
    if db.execute(select(User.id)).first():
        return 0
    for username, password, role, full_name, department in DEFAULT_USERS:
        create_user(db, username=username, password=password, role=role,
                    full_name=full_name, department=department)
    return len(DEFAULT_USERS)
