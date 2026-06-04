"""Authentication endpoints — login, logout, and the current user."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from .. import auth, schemas
from ..config import get_settings
from ..database import get_db

router = APIRouter(prefix="/auth", tags=["auth"])
settings = get_settings()


def _user_out(user) -> schemas.UserOut:
    return schemas.UserOut(username=user.username, full_name=user.full_name,
                           role=user.role, department=user.department or "",
                           capabilities=sorted(auth.role_caps(user.role)))


@router.post("/login", response_model=schemas.UserOut)
def login(payload: schemas.LoginIn, response: Response, db: Session = Depends(get_db)):
    user = auth.authenticate(db, payload.username, payload.password)
    if not user:
        raise HTTPException(401, "Invalid username or password.")
    token = auth.create_session(db, user)
    response.set_cookie(
        settings.session_cookie_name, token, httponly=True, samesite="lax",
        secure=settings.session_cookie_secure, path="/",
        max_age=settings.session_ttl_hours * 3600,
    )
    return _user_out(user)


@router.post("/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    auth.delete_session(db, request.cookies.get(settings.session_cookie_name))
    response.delete_cookie(settings.session_cookie_name, path="/")
    return {"ok": True}


@router.get("/me", response_model=schemas.UserOut)
def me(user=Depends(auth.current_user)):
    return _user_out(user)
