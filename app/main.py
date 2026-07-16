"""FastAPI application: upload -> review -> manual fill -> export."""

import hmac
import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import Settings, get_settings
from app.dayn_csv import DayNCsvError, export, parse
from app.mapper import load_mappings, map_document_block_results
from app.matcher import DeviceMatch, match_devices
from app.netbox_client import NetBoxClient, NetBoxError
from app.runtime_settings import RuntimeSettings, hash_password, verify_password
from app.store import SessionStore, UploadSession

logger = logging.getLogger("app")

settings = get_settings()
logging.basicConfig(
    level=settings.log_level.upper(),
    format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
)

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="DayN-NetBox Bridge")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
store = SessionStore(ttl_seconds=settings.session_ttl_seconds)
runtime = RuntimeSettings(Path(settings.runtime_settings_path))
basic_auth = HTTPBasic(auto_error=False)


def current_settings() -> Settings:
    """Environment settings with GUI-saved overrides applied."""
    return runtime.effective(settings)


def _admin_configured() -> bool:
    return bool(settings.admin_password or runtime.get("admin_password_hash"))


def require_admin(
    credentials: HTTPBasicCredentials | None = Depends(basic_auth),
) -> None:
    """Basic auth for the settings GUI.

    Until an admin password exists (env or GUI) the page is open, so the
    first visit can set one — the page shows a prominent warning.
    """
    if not _admin_configured():
        return
    expected_user = str(runtime.get("admin_username") or settings.admin_username)
    unauthorized = HTTPException(
        status_code=401,
        detail="Admin login required.",
        headers={"WWW-Authenticate": 'Basic realm="DayN-NetBox Bridge settings"'},
    )
    if credentials is None or credentials.username != expected_user:
        raise unauthorized
    stored_hash = runtime.get("admin_password_hash")
    if settings.admin_password:
        if not secrets_compare(credentials.password, settings.admin_password):
            raise unauthorized
    elif not (stored_hash and verify_password(credentials.password, str(stored_hash))):
        raise unauthorized


def secrets_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


class UploadError(Exception):
    """User-facing upload problem (too large, unparseable, ...)."""


@app.exception_handler(UploadError)
async def upload_error_handler(request: Request, exc: UploadError) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "upload.html", {"error": str(exc)}, status_code=400
    )


@app.exception_handler(DayNCsvError)
async def csv_error_handler(request: Request, exc: DayNCsvError) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "upload.html", {"error": f"Could not parse the file: {exc}"}, status_code=400
    )


@app.exception_handler(Exception)
async def generic_error_handler(request: Request, exc: Exception) -> HTMLResponse:
    logger.error("unhandled error on %s: %s", request.url.path, exc)
    return templates.TemplateResponse(
        request,
        "error.html",
        {"message": "An unexpected error occurred. Please try again."},
        status_code=500,
    )


def _session_or_404(upload_id: str) -> UploadSession:
    session = store.get(upload_id)
    if session is None:
        raise UploadError("This upload session has expired or does not exist. Upload again.")
    return session


def _initial_results(session: UploadSession) -> None:
    mappings = load_mappings()
    session.block_results = map_document_block_results(
        session.document.blocks, session.matches, mappings, None
    )


def _view_model(session: UploadSession) -> dict[str, Any]:
    """Group blocks by device and compute the summary banner counts."""
    devices: dict[str, dict[str, Any]] = {}
    for index, block in enumerate(session.document.blocks):
        entry = devices.setdefault(
            block.device_name,
            {
                "name": block.device_name,
                "match": session.matches.get(block.device_name),
                "blocks": [],
            },
        )
        entry["blocks"].append(
            {
                "index": index,
                "template": block.template,
                "template_short": block.template.split(":")[-1],
                "results": session.block_results[index],
            }
        )

    counts = {"auto": 0, "manual": 0, "conflict": 0, "ambiguous": 0, "file": 0, "total": 0}
    empty_final = 0
    for results in session.block_results:
        for result in results.values():
            counts["total"] += 1
            key = "manual" if result.status in ("manual", "missing") else result.status
            counts[key] += 1
            if not result.final_value:
                empty_final += 1
    not_found = sum(
        1 for m in session.matches.values() if m.status in ("not_found", "netbox_unreachable")
    )
    ambiguous_devices = [m for m in session.matches.values() if m.status == "ambiguous"]

    return {
        "session": session,
        "devices": list(devices.values()),
        "counts": counts,
        "empty_final": empty_final,
        "devices_not_found": not_found,
        "ambiguous_devices": ambiguous_devices,
        "netbox_configured": bool(
            current_settings().netbox_url and current_settings().netbox_token
        ),
    }


@app.get("/", response_class=HTMLResponse)
def upload_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "upload.html", {})


@app.post("/upload")
async def upload(request: Request, file: UploadFile) -> RedirectResponse:
    data = await file.read()
    if len(data) > settings.upload_max_bytes:
        raise UploadError(
            f"File is larger than {settings.upload_max_bytes // (1024 * 1024)} MB."
        )
    document = parse(data)
    session = store.create(filename=file.filename or "upload.csv", document=document)
    _initial_results(session)
    logger.info("upload_id=%s parsed blocks=%d", session.id, len(document.blocks))
    return RedirectResponse(url=f"/review/{session.id}", status_code=303)


@app.get("/review/{upload_id}", response_class=HTMLResponse)
def review(request: Request, upload_id: str) -> HTMLResponse:
    session = _session_or_404(upload_id)
    return templates.TemplateResponse(request, "review.html", _view_model(session))


@app.post("/review/{upload_id}/fill", response_class=HTMLResponse)
async def fill(request: Request, upload_id: str) -> HTMLResponse:
    session = _session_or_404(upload_id)
    form = await request.form()

    manual_values = _collect_manual_values(session)
    session.fill_error = ""
    try:
        client = NetBoxClient(current_settings())
        session.matches = match_devices(session.document.device_names(), client)
        _apply_device_picks(session, form)
        matched_records = [
            m.record
            for m in session.matches.values()
            if m.status == "matched" and m.record is not None
        ]
        client.prefetch_device_details([int(r.id) for r in matched_records])
        client.prefetch_site_details(
            sorted(
                {
                    site_id
                    for r in matched_records
                    if (site_id := _record_site_id(r)) is not None
                }
            )
        )
        session.block_results = map_document_block_results(
            session.document.blocks, session.matches, load_mappings(), client
        )
    except NetBoxError as exc:
        session.fill_error = str(exc)
        client = None
        session.block_results = map_document_block_results(
            session.document.blocks, session.matches, load_mappings(), None
        )
    _restore_manual_values(session, manual_values)
    session.filled = True
    logger.info(
        "upload_id=%s fill matched=%d",
        session.id,
        sum(1 for m in session.matches.values() if m.status == "matched"),
    )
    return templates.TemplateResponse(request, "partials/review_body.html", _view_model(session))


def _record_site_id(record: object) -> int | None:
    data = getattr(record, "__dict__", {})
    site = data.get("site")
    raw = getattr(site, "__dict__", {}).get("id") if site is not None else None
    return int(raw) if raw is not None else None


def _collect_manual_values(session: UploadSession) -> dict[tuple[int, str], str]:
    return {
        (i, var): r.manual_value
        for i, results in enumerate(session.block_results)
        for var, r in results.items()
        if r.manual_value is not None
    }


def _restore_manual_values(
    session: UploadSession, values: dict[tuple[int, str], str]
) -> None:
    for (i, var), value in values.items():
        if i < len(session.block_results) and var in session.block_results[i]:
            session.block_results[i][var].manual_value = value


def _apply_device_picks(session: UploadSession, form: Any) -> None:
    """Resolve ambiguous device matches the user picked in the GUI."""
    for name, match in session.matches.items():
        if match.status != "ambiguous":
            continue
        picked = form.get(f"device_pick_{name}")
        if not picked:
            continue
        for candidate in match.candidates:
            if str(candidate.id) == str(picked):
                session.matches[name] = DeviceMatch(
                    name=name, status="matched", record=candidate
                )
                break


@app.post("/review/{upload_id}/value", response_class=HTMLResponse)
async def set_value(
    request: Request,
    upload_id: str,
    block_index: int = Form(...),
    variable: str = Form(...),
    value: str = Form(""),
    apply_all: bool = Form(False),
) -> HTMLResponse:
    session = _session_or_404(upload_id)
    targets = (
        range(len(session.block_results)) if apply_all else [block_index]
    )
    for index in targets:
        if 0 <= index < len(session.block_results):
            result = session.block_results[index].get(variable)
            if result is not None:
                result.manual_value = value
    return templates.TemplateResponse(request, "partials/review_body.html", _view_model(session))


@app.get("/export/{upload_id}")
def export_csv(upload_id: str) -> Response:
    session = _session_or_404(upload_id)
    session.apply_final_values()
    data = export(session.document)
    stem = session.filename.rsplit(".", 1)[0] or "export"
    filename = f"{stem}_enriched.csv"
    logger.info("upload_id=%s export bytes=%d", session.id, len(data))
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{quote(filename)}"',
        },
    )


@app.get("/healthz")
def healthz() -> dict[str, str]:
    active = current_settings()
    netbox = "unconfigured"
    if active.netbox_url and active.netbox_token:
        try:
            netbox = "ok" if NetBoxClient(active).ping() else "unreachable"
        except NetBoxError:
            netbox = "unreachable"
    return {"status": "ok", "netbox": netbox}


def _settings_view(
    request: Request,
    message: str = "",
    error: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    active = current_settings()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "netbox_url": active.netbox_url,
            "token_set": bool(active.netbox_token),
            "verify_ssl": active.netbox_verify_ssl,
            "admin_username": str(runtime.get("admin_username") or settings.admin_username),
            "admin_configured": _admin_configured(),
            "password_from_env": bool(settings.admin_password),
            "message": message,
            "error": error,
        },
        status_code=status_code,
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, _: None = Depends(require_admin)) -> HTMLResponse:
    return _settings_view(request)


@app.post("/settings", response_class=HTMLResponse)
def save_settings(
    request: Request,
    netbox_url: str = Form(""),
    netbox_token: str = Form(""),
    netbox_verify_ssl: bool = Form(False),
    admin_username: str = Form(""),
    new_password: str = Form(""),
    new_password_confirm: str = Form(""),
    _: None = Depends(require_admin),
) -> HTMLResponse:
    if new_password or new_password_confirm:
        if settings.admin_password:
            return _settings_view(
                request,
                error="The admin password is set via the ADMIN_PASSWORD environment "
                "variable and cannot be changed here.",
                status_code=400,
            )
        if new_password != new_password_confirm:
            return _settings_view(request, error="Passwords do not match.", status_code=400)
        if len(new_password) < 8:
            return _settings_view(
                request, error="Password must be at least 8 characters.", status_code=400
            )

    updates: dict[str, object] = {
        "netbox_url": netbox_url.strip().rstrip("/"),
        "netbox_verify_ssl": netbox_verify_ssl,
    }
    if netbox_token.strip():  # empty field means "keep the stored token"
        updates["netbox_token"] = netbox_token.strip()
    if admin_username.strip():
        updates["admin_username"] = admin_username.strip()
    if new_password and not settings.admin_password:
        updates["admin_password_hash"] = hash_password(new_password)
    runtime.save(**updates)
    logger.info("settings updated (netbox_url=%s)", updates["netbox_url"])
    message = "Settings saved."
    if "admin_password_hash" in updates:
        message = "Settings saved. Admin password set — the next request will ask you to log in."
    return _settings_view(request, message=message)


@app.post("/settings/test", response_class=HTMLResponse)
def test_netbox_connection(
    request: Request,
    netbox_url: str = Form(""),
    netbox_token: str = Form(""),
    netbox_verify_ssl: bool = Form(False),
    _: None = Depends(require_admin),
) -> HTMLResponse:
    """Test the connection with the form values (falling back to saved ones)."""
    active = current_settings()
    candidate = active.model_copy(
        update={
            "netbox_url": netbox_url.strip().rstrip("/") or active.netbox_url,
            "netbox_token": netbox_token.strip() or active.netbox_token,
            "netbox_verify_ssl": netbox_verify_ssl,
        }
    )
    context: dict[str, object]
    try:
        status = NetBoxClient(candidate).status()
        version = str(status.get("netbox-version") or "unknown version")
        context = {"ok": True, "detail": f"Connected — NetBox {version}."}
    except NetBoxError as exc:
        context = {"ok": False, "detail": str(exc)}
    return templates.TemplateResponse(request, "partials/test_result.html", context)
