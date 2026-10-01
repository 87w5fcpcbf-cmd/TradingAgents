"""Strategy-tester dashboard: every signal, its Jev probabilities, the result. Login required.

Fails closed: without DASHBOARD_PASSWORD it serves nothing. Everything rendered is HTML-escaped.
Bind to localhost, your LAN or a private tunnel (e.g. Tailscale); never expose it to the open internet.
"""
from __future__ import annotations

import hmac
import html
import os
import secrets

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from .config import Config
from .db import Db
from .report import daily
from .risk import KILL

E = html.escape

PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Jev bot</title><style>
body{{font:14px/1.4 ui-monospace,Menlo,monospace;background:#0b1220;color:#d6e2f0;margin:0;padding:16px}}
h1,h2{{font-weight:600}} table{{border-collapse:collapse;width:100%;margin-bottom:24px}}
th,td{{border-bottom:1px solid #223;padding:6px 8px;text-align:left;vertical-align:top}}
.ok{{color:#7ee787}} .bad{{color:#ff7b72}} pre{{background:#101a2e;padding:12px;overflow:auto}}
.tag{{border:1px solid #f0b72f;color:#f0b72f;padding:2px 6px;border-radius:4px;font-size:12px}}
</style><h1>Jev trading bot <span class=tag>PAPER ONLY</span> <span class=tag>NOT FINANCIAL ADVICE</span></h1>{body}"""


def _login_page(msg: str = "") -> str:
    return PAGE.format(body=f"<p class=bad>{E(msg)}</p><form method=post action=/login>"
                            "<input type=password name=password autofocus> <button>Log in</button></form>")


def create_app(db: Db, cfg: Config) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=os.environ.get("DASHBOARD_SECRET") or secrets.token_hex(32),
                       same_site="strict", https_only=False, max_age=8 * 3600)

    def password() -> str | None:
        return os.environ.get("DASHBOARD_PASSWORD") or None

    def authed(req: Request) -> bool:
        return bool(password()) and req.session.get("ok") is True

    @app.get("/login", response_class=HTMLResponse)
    def login_form():
        if not password():
            return HTMLResponse(_login_page("Dashboard disabled: DASHBOARD_PASSWORD is not set."), status_code=503)
        return HTMLResponse(_login_page())

    @app.post("/login")
    def login(req: Request, password_in: str = Form("", alias="password")):
        pw = password()
        if not pw:
            return HTMLResponse(_login_page("Dashboard disabled: DASHBOARD_PASSWORD is not set."), status_code=503)
        if not hmac.compare_digest(password_in.encode(), pw.encode()):
            return HTMLResponse(_login_page("Wrong password."), status_code=401)
        req.session["ok"] = True
        return RedirectResponse("/", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def home(req: Request):
        if not authed(req):
            return RedirectResponse("/login", status_code=303)
        killed = db.get_flag(KILL)
        kill = (f"<span class=bad>TRIPPED: {E(str(db.flag_reason(KILL)))}</span>" if killed else "<span class=ok>ok</span>")
        eq = db.equity_series()
        last_eq = f"${eq[-1][1]:,.2f}" if eq else "n/a"
        rows = "".join(
            f"<tr><td>{E(str(s.get('id')))}</td><td>{E(str(s.get('symbol')))}</td>"
            f"<td>{'yes' if s.get('fired') else 'no'}</td>"
            f"<td>{E(', '.join(f'{k}={v:.2f}' for k, v in (s.get('probabilities') or {}).items()))}</td>"
            f"<td>{E(str(s.get('model')))}</td><td>{E(str(s.get('result')))}</td></tr>"
            for s in reversed(db.signals()[-200:]))
        trades = "".join(
            f"<tr><td>{E(str(t.get('symbol')))}</td><td>{E(str(t.get('qty')))}</td><td>{E(str(t.get('price')))}</td>"
            f"<td>{E(str(t.get('pnl')))}</td></tr>" for t in reversed(db.trades()[-100:]))
        body = (f"<p>Kill switch: {kill} &middot; Equity: {E(last_eq)}</p>"
                f"<h2>Signals (Jev probabilities for the direction-matching answer)</h2>"
                f"<table><tr><th>#</th><th>Symbol</th><th>Fired</th><th>Probabilities</th><th>Jev model</th><th>Result</th></tr>{rows}</table>"
                f"<h2>Trades</h2><table><tr><th>Symbol</th><th>Qty</th><th>Price</th><th>P&amp;L</th></tr>{trades}</table>"
                f"<h2>Today</h2><pre>{E(daily(db))}</pre>")
        return HTMLResponse(PAGE.format(body=body))

    @app.get("/api/signals")
    def api_signals(req: Request):
        if not authed(req):
            return HTMLResponse("unauthorized", status_code=401)
        return db.signals()[-200:]

    return app
