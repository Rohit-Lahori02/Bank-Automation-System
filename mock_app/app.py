"""Harbor Federal Credit Union - Member Servicing Console (mock).

A deliberately legacy-looking, server-rendered back-office app used as the
automation target. Design goals:

* No test IDs, no semantic markup, table-based layout, labels not associated
  with inputs, an iframe for the accounts panel - i.e. a hostile surface.
* A multi-step business flow: sign on -> member inquiry -> profile -> open
  sub-account -> review -> confirm.
* Every runtime condition the replay engine must handle is reachable on
  demand: not-found, permission denied, validation error, session expiry,
  slow load, unexpected dialog, application error (see chaos.py).

Nothing here is real. Credentials come from environment variables so the
automation layer can treat them as secret references.
"""

from __future__ import annotations

import asyncio
import secrets
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .chaos import ChaosController
from .config import Settings, load_settings
from .data import MIN_INITIAL_DEPOSIT, PRODUCTS, MemberStore, money, parse_money
from .sessions import COOKIE_NAME, SessionStore

BASE_DIR = Path(__file__).parent
APP_VERSION = "4.2.1"


class NotAuthenticated(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def create_app(
    settings: Settings | None = None,
    chaos: ChaosController | None = None,
    store: MemberStore | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    chaos = chaos or ChaosController()
    store = store or MemberStore()
    sessions = SessionStore(idle_seconds=settings.session_idle_seconds)

    app = FastAPI(title="HFCU Member Servicing Console (mock)", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.chaos = chaos
    app.state.store = store
    app.state.sessions = sessions

    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=BASE_DIR / "templates")
    templates.env.filters["money"] = money

    # ------------------------------------------------------------------ helpers
    def render(request: Request, name: str, status_code: int = 200, *, full_page: bool = True, **ctx):
        session = getattr(request.state, "session", None)
        dialog = chaos.consume("maintenance_dialog") if full_page else False
        context = {"session": session, "dialog": dialog, "app_version": APP_VERSION, **ctx}
        return templates.TemplateResponse(request, name, context, status_code=status_code)

    def require_session(request: Request) -> dict:
        sid = request.cookies.get(COOKIE_NAME)
        session = sessions.get(sid)
        if session is None:
            raise NotAuthenticated("expired" if sid else "login")
        if chaos.consume("expire_session"):
            sessions.delete(sid)
            raise NotAuthenticated("expired")
        request.state.session = session
        return session

    def error_page(request: Request, status_code: int, title: str, code: str, message: str):
        return render(request, "error.html", status_code, title=title, code=code, message=message)

    # --------------------------------------------------------------- middleware
    @app.middleware("http")
    async def chaos_delay(request: Request, call_next):
        delay = chaos.slow_ms
        if delay and not request.url.path.startswith(("/__chaos", "/static")):
            await asyncio.sleep(delay / 1000)
        return await call_next(request)

    @app.exception_handler(NotAuthenticated)
    async def _redirect_to_login(request: Request, exc: NotAuthenticated):
        response = RedirectResponse(f"/login?reason={exc.reason}", status_code=303)
        if exc.reason == "expired":
            response.delete_cookie(COOKIE_NAME)
        return response

    # -------------------------------------------------------------------- auth
    @app.get("/", response_class=HTMLResponse)
    async def root(request: Request):
        if sessions.get(request.cookies.get(COOKIE_NAME)):
            return RedirectResponse("/console", status_code=303)
        return RedirectResponse("/login", status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request, reason: str = ""):
        messages = {
            "expired": "Your session has expired. Please sign on again.",
            "signedoff": "You have been signed off.",
        }
        return render(request, "login.html", notice=messages.get(reason, ""), error="")

    @app.post("/login", response_class=HTMLResponse)
    async def login_submit(request: Request, userid: str = Form(""), passwd: str = Form("")):
        if userid.strip() == settings.username and passwd == settings.password:
            sid = sessions.create(userid.strip())
            response = RedirectResponse("/console", status_code=303)
            response.set_cookie(COOKIE_NAME, sid, httponly=True, samesite="lax")
            return response
        return render(request, "login.html", notice="", error="Invalid user ID or password. (SEC-0401)")

    @app.get("/logout")
    async def logout(request: Request):
        sessions.delete(request.cookies.get(COOKIE_NAME))
        response = RedirectResponse("/login?reason=signedoff", status_code=303)
        response.delete_cookie(COOKIE_NAME)
        return response

    # ----------------------------------------------------------------- console
    @app.get("/console", response_class=HTMLResponse)
    async def console(request: Request):
        require_session(request)
        return render(request, "console.html")

    # ---------------------------------------------------------- member inquiry
    @app.get("/members/search", response_class=HTMLResponse)
    async def search_form(request: Request):
        require_session(request)
        return render(request, "search.html", q="", error="", result=None, not_found=False)

    @app.post("/members/search", response_class=HTMLResponse)
    async def search_submit(request: Request, q: str = Form("")):
        require_session(request)
        q = q.strip()
        if not (q.isdigit() and len(q) == 5):
            return render(request, "search.html", q=q, error="Member number must be exactly 5 digits. (VAL-1001)",
                          result=None, not_found=False)
        if q in settings.restricted_members:
            return error_page(request, 403, "Access Denied", "SEC-0403",
                              "Your user role does not permit viewing this member record. Contact your supervisor.")
        member = store.get(q)
        if member is None:
            return render(request, "search.html", q=q, error="", result=None, not_found=True)
        return render(request, "search.html", q=q, error="", result=member, not_found=False)

    @app.get("/members/{member_id}", response_class=HTMLResponse)
    async def member_profile(request: Request, member_id: str):
        require_session(request)
        if member_id in settings.restricted_members:
            return error_page(request, 403, "Access Denied", "SEC-0403",
                              "Your user role does not permit viewing this member record. Contact your supervisor.")
        if member_id in settings.crashing_members or chaos.consume("app_error"):
            return error_page(request, 500, "Application Error", "CLK-0500",
                              "An unexpected error occurred while processing your request "
                              f"(ORA-01555: snapshot too old). Reference: {secrets.token_hex(4).upper()}. "
                              "Contact the help desk.")
        member = store.get(member_id)
        if member is None:
            return error_page(request, 404, "Record Not Found", "REC-0404",
                              f"Member record {member_id} was not found.")
        return render(request, "member.html", member=member)

    @app.get("/members/{member_id}/accounts", response_class=HTMLResponse)
    async def accounts_frame(request: Request, member_id: str, closed: str = ""):
        require_session(request)
        member = store.get(member_id)
        if member is None:
            return HTMLResponse("<html><body><font color=red>Record not found.</font></body></html>", status_code=404)
        return render(request, "accounts_frame.html", full_page=False, member=member, closed=closed)

    # ------------------------------------------------------------ sub-account
    @app.get("/members/{member_id}/subaccount/new", response_class=HTMLResponse)
    async def subaccount_form(request: Request, member_id: str):
        session = require_session(request)
        member = store.get(member_id)
        if member is None:
            return error_page(request, 404, "Record Not Found", "REC-0404", f"Member record {member_id} was not found.")
        pending = session["pending"].get(member_id, {})
        return render(request, "subaccount_form.html", member=member, products=PRODUCTS, errors=[],
                      form={"product": pending.get("product", "S-VAC"), "nickname": pending.get("nickname", ""),
                            "deposit": pending.get("deposit", "")})

    @app.post("/members/{member_id}/subaccount/new", response_class=HTMLResponse)
    async def subaccount_submit(request: Request, member_id: str, product: str = Form(""),
                                nickname: str = Form(""), deposit: str = Form("")):
        session = require_session(request)
        member = store.get(member_id)
        if member is None:
            return error_page(request, 404, "Record Not Found", "REC-0404", f"Member record {member_id} was not found.")
        errors: list[str] = []
        nickname = nickname.strip()
        if product not in PRODUCTS:
            errors.append("Select a valid product type. (VAL-2001)")
        if not (2 <= len(nickname) <= 30):
            errors.append("Nickname is required and must be 2 to 30 characters. (VAL-2002)")
        amount = parse_money(deposit)
        if amount is None:
            errors.append("Initial deposit must be a dollar amount. (VAL-2003)")
        elif amount < MIN_INITIAL_DEPOSIT:
            errors.append(f"Initial deposit must be at least {money(MIN_INITIAL_DEPOSIT)}. (VAL-2004)")
        if errors:
            return render(request, "subaccount_form.html", member=member, products=PRODUCTS, errors=errors,
                          form={"product": product, "nickname": nickname, "deposit": deposit})
        session["pending"][member_id] = {"product": product, "nickname": nickname, "deposit": str(amount)}
        return RedirectResponse(f"/members/{member_id}/subaccount/review", status_code=303)

    @app.get("/members/{member_id}/subaccount/review", response_class=HTMLResponse)
    async def subaccount_review(request: Request, member_id: str):
        session = require_session(request)
        member = store.get(member_id)
        pending = session["pending"].get(member_id)
        if member is None or not pending:
            return RedirectResponse(f"/members/{member_id}/subaccount/new", status_code=303)
        return render(request, "subaccount_review.html", member=member, pending=pending,
                      product_name=PRODUCTS[pending["product"]])

    @app.post("/members/{member_id}/subaccount/confirm", response_class=HTMLResponse)
    async def subaccount_confirm(request: Request, member_id: str):
        session = require_session(request)
        pending = session["pending"].pop(member_id, None)
        if not pending or not store.exists(member_id):
            return RedirectResponse(f"/members/{member_id}/subaccount/new", status_code=303)
        result = store.open_subaccount(member_id, pending["product"], pending["nickname"], parse_money(pending["deposit"]))
        session["pending"][f"confirmed:{member_id}"] = result
        return RedirectResponse(f"/members/{member_id}/subaccount/confirmed?ref={result['confirmation_no']}", status_code=303)

    @app.get("/members/{member_id}/subaccount/confirmed", response_class=HTMLResponse)
    async def subaccount_confirmed(request: Request, member_id: str, ref: str = ""):
        session = require_session(request)
        member = store.get(member_id)
        result = session["pending"].get(f"confirmed:{member_id}")
        if member is None or not result or result["confirmation_no"] != ref:
            return error_page(request, 404, "Record Not Found", "REC-0404", "No confirmation on file for this reference.")
        return render(request, "subaccount_confirmed.html", member=member, result=result)

    # ----------------------------------------------------------- close account
    @app.get("/members/{member_id}/accounts/{account_no}/close", response_class=HTMLResponse)
    async def close_account_form(request: Request, member_id: str, account_no: str):
        require_session(request)
        member = store.get(member_id)
        account = next((a for a in (member or {}).get("accounts", []) if a["account_no"] == account_no), None)
        if account is None:
            return error_page(request, 404, "Record Not Found", "REC-0404", f"Account {account_no} was not found.")
        return render(request, "close_account.html", member=member, account=account)

    @app.post("/members/{member_id}/accounts/{account_no}/close")
    async def close_account_submit(request: Request, member_id: str, account_no: str):
        require_session(request)
        if not store.close_account(member_id, account_no):
            return error_page(request, 404, "Record Not Found", "REC-0404", f"Account {account_no} was not found or already closed.")
        return RedirectResponse(f"/members/{member_id}/accounts?closed={account_no}", status_code=303)

    # ----------------------------------------------------------------- chaos
    @app.get("/__chaos")
    async def chaos_state():
        return JSONResponse(chaos.snapshot())

    @app.post("/__chaos")
    async def chaos_update(request: Request):
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            payload = await request.json()
        else:
            payload = dict(await request.form())
        try:
            return JSONResponse(chaos.update(**payload))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @app.post("/__chaos/reset")
    async def chaos_reset():
        return JSONResponse(chaos.reset())

    return app


app = create_app()
