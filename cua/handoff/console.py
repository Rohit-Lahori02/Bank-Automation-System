"""Operator console: a minimal, real surface for the human side of a handoff.

Deliberately bare. It lists intervention requests, shows one with its context
(reason, step, screenshot, screen listing, what the human has done so far),
and offers three decisions. The manual work itself happens in the live
browser window the automation is driving - this console is the seam, not a
co-browsing product (see REPORT.md for the design of that).
"""

from __future__ import annotations

import html
import threading
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from .controller import HandoffController

STYLE = """
<style>
body{font-family:Segoe UI,system-ui,sans-serif;margin:0;background:#eef2f7;color:#1e2733}
header{background:#0b2a5b;color:#fff;padding:14px 24px;font-size:18px;font-weight:600}
main{padding:20px 24px;max-width:1100px}
.card{background:#fff;border:1px solid #dbe2ec;border-radius:10px;padding:18px 22px;margin-bottom:16px}
table{border-collapse:collapse;width:100%;font-size:14px}td,th{padding:8px 10px;border-bottom:1px solid #edf1f6;text-align:left}
th{color:#41506a;font-size:12px;text-transform:uppercase}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600}
.pending{background:#fff6e5;color:#7a4d00}.claimed{background:#e9f1ff;color:#0f3470}
.approved,.resumed{background:#e8f7ee;color:#135c2f}.aborted,.timeout{background:#fdecec;color:#8f1d17}
pre{background:#f7f9fc;border:1px solid #e6ebf2;border-radius:8px;padding:12px;font-size:12px;max-height:380px;overflow:auto}
button{font:inherit;font-weight:600;padding:9px 18px;border-radius:6px;border:1px solid #16418a;background:#16418a;color:#fff;cursor:pointer;margin-right:8px}
button.secondary{background:#fff;color:#1e2733;border-color:#c5cfdd}button.danger{background:#b3261e;border-color:#b3261e}
img{max-width:100%;border:1px solid #dbe2ec;border-radius:8px}.muted{color:#6b7a90;font-size:13px}
.k{color:#6b7a90;width:150px}
</style>"""


def create_console(controller: HandoffController) -> FastAPI:
    app = FastAPI(title="CUA Operator Console", docs_url=None, redoc_url=None)

    def page(title: str, body: str) -> HTMLResponse:
        return HTMLResponse(f"<html><head><title>{html.escape(title)}</title>{STYLE}</head><body>"
                            f"<header>Operator Console · Computer-Use Automation</header><main>{body}</main></body></html>")

    @app.get("/", response_class=HTMLResponse)
    def index():
        rows = "".join(
            f"<tr><td><a href='/interventions/{r.id}'>{r.id}</a></td><td>{html.escape(r.run_kind)} {html.escape(r.run_id)}</td>"
            f"<td>{html.escape(r.step_id or '-')}</td><td>{html.escape(r.kind)}</td>"
            f"<td><span class='pill {r.status}'>{r.status}</span></td><td>{html.escape(r.reason[:90])}</td></tr>"
            for r in sorted(controller.interventions.values(), key=lambda r: -r.created_at)
        ) or "<tr><td colspan=6 class='muted'>No intervention requests yet.</td></tr>"
        body = (f"<div class='card'><table><tr><th>Request</th><th>Run</th><th>Step</th><th>Kind</th><th>Status</th>"
                f"<th>Reason</th></tr>{rows}</table></div>"
                f"<div class='muted'>Session control: <b>{controller.control.holder.value}</b>. "
                f"This page refreshes every 3 seconds.</div><script>setTimeout(()=>location.reload(),3000)</script>")
        return page("Interventions", body)

    @app.get("/interventions/{iid}", response_class=HTMLResponse)
    def detail(iid: str):
        if iid not in controller.interventions:
            raise HTTPException(404)
        r = controller.claim(iid) if controller.interventions[iid].status == "pending" else controller.interventions[iid]
        acts = "".join(f"<li>{html.escape(a.render())} <span class='muted'>@ {html.escape(a.url)}</span></li>"
                       for a in r.human_actions) or "<li class='muted'>nothing captured yet</li>"
        shot = f"<img src='/interventions/{r.id}/screenshot' alt='screenshot at escalation'>" if r.screenshot else ""
        buttons = "" if r.status not in ("pending", "claimed") else f"""
          <form method='post' action='/interventions/{r.id}/decision'>
            <input type='hidden' name='token' value='{html.escape(r.control_token)}'>
            <button name='decision' value='resumed'>I did the manual steps — resume automation</button>
            <button name='decision' value='approved' class='secondary'>Approve: let automation perform the step</button>
            <button name='decision' value='aborted' class='danger'>Abort run</button>
          </form>"""
        body = f"""
        <div class='card'>
          <table>
            <tr><td class='k'>Request</td><td>{r.id} <span class='pill {r.status}'>{r.status}</span></td></tr>
            <tr><td class='k'>Run</td><td>{html.escape(r.run_kind)} · {html.escape(r.run_id)} · {html.escape(r.capability_id)}</td></tr>
            <tr><td class='k'>Goal</td><td>{html.escape(r.goal)}</td></tr>
            <tr><td class='k'>Stopped at</td><td>{html.escape(r.step_id or '-')} ({html.escape(r.kind)})</td></tr>
            <tr><td class='k'>Why</td><td>{html.escape(r.reason)}</td></tr>
            <tr><td class='k'>Page</td><td>{html.escape(r.url)}</td></tr>
            <tr><td class='k'>Control</td><td><b>{controller.control.holder.value}</b> · token {html.escape(r.control_token[:6])}…</td></tr>
            <tr><td class='k'>Live session</td><td>{html.escape(r.session_url) or '(headed window on this machine)'}
              {"<span class='muted'> · a remote operator client can attach to this endpoint</span>" if r.session_url else ""}</td></tr>
          </table>
        </div>
        <div class='card'><b>What to do</b><p class='muted'>The live browser window is open on this machine and is now
          yours. Perform the manual steps there (or fix whatever blocked the automation), then come back and choose a
          decision. Everything you do in that window is recorded into the run's evidence.</p>{buttons}</div>
        <div class='card'><b>Captured human actions</b><ul id='acts'>{acts}</ul></div>
        <div class='card'><b>Screen at escalation</b>{shot}<pre>{html.escape(r.screen)}</pre></div>
        <script>
        {"" if r.status not in ("pending", "claimed") else "setInterval(async()=>{const d=await (await fetch('/api/interventions/"+r.id+"')).json();"
         "document.getElementById('acts').innerHTML=d.human_actions.length?d.human_actions.map(a=>'<li>'+a.type+' '+a.role+' \"'+a.name+'\"'+(a.value!=null&&a.type==='input'?' = \"'+a.value+'\"':'')+'</li>').join(''):'<li class=muted>nothing captured yet</li>';"
         "if(!['pending','claimed'].includes(d.status))location.reload();},1500)"}
        </script>"""
        return page(f"Intervention {r.id}", body)

    @app.get("/interventions/{iid}/screenshot")
    def screenshot(iid: str):
        r = controller.interventions.get(iid)
        if not r or not r.screenshot or not Path(r.screenshot).exists():
            raise HTTPException(404)
        return FileResponse(r.screenshot)

    @app.post("/interventions/{iid}/decision")
    def decide(iid: str, decision: str = Form(...), token: str = Form("")):
        if iid not in controller.interventions:
            raise HTTPException(404)
        try:
            controller.decide(iid, decision, token=token or None)
        except PermissionError:
            raise HTTPException(409, "stale control token")
        except ValueError:
            raise HTTPException(400, "unknown decision")
        return RedirectResponse(f"/interventions/{iid}", status_code=303)

    @app.get("/api/interventions")
    def api_list():
        return JSONResponse([r.model_dump(mode="json", exclude={"screen"}) for r in controller.interventions.values()])

    @app.get("/api/interventions/{iid}")
    def api_detail(iid: str):
        r = controller.interventions.get(iid)
        if not r:
            raise HTTPException(404)
        return JSONResponse(r.model_dump(mode="json"))

    @app.post("/api/interventions/{iid}/decision")
    async def api_decide(iid: str, request: Request):
        payload = await request.json()
        if iid not in controller.interventions:
            raise HTTPException(404)
        try:
            r = controller.decide(iid, payload.get("decision", "resumed"), token=payload.get("token"))
        except PermissionError:
            raise HTTPException(409, "stale control token")
        return JSONResponse({"id": r.id, "status": r.status})

    return app


class ConsoleServer:
    """Run the operator console in a background thread of the automation process."""

    def __init__(self, controller: HandoffController, host: str = "127.0.0.1", port: int = 8001) -> None:
        import uvicorn

        self.host, self.port = host, port
        self.app = create_console(controller)
        self._server = uvicorn.Server(uvicorn.Config(self.app, host=host, port=port, log_level="warning"))
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> "ConsoleServer":
        self._thread.start()
        deadline = __import__("time").time() + 10
        while not self._server.started and __import__("time").time() < deadline:
            __import__("time").sleep(0.05)
        return self

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)
