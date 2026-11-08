"""Public Flight Deck pages: landing, recorded replay, and live evidence."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app import site_data

WEB_DIR = Path(__file__).resolve().parent / "web"
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))

GITHUB_URL = "https://github.com/saiteja-018/flowscout"


def _seconds(value: Any) -> str:
    if value is None or value == "":
        return "—"
    try:
        return f"{float(value):.2f} s"
    except (TypeError, ValueError):
        return str(value)


def _number(value: Any) -> str:
    if value is None or value == "":
        return "—"
    try:
        return f"{float(value):,.0f}"
    except (TypeError, ValueError):
        return str(value)


def _money(value: Any) -> str:
    if value is None or value == "":
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    sign = "-" if number < 0 else ""
    return f"{sign}${abs(number):,.2f}"


def _date(value: Any) -> str:
    if not value:
        return "—"
    text = str(value)
    if "T" in text:
        day, rest = text.split("T", 1)
        return f"{day} {rest[:8]} UTC"
    return text


def _short_commit(value: Any) -> str:
    if not value:
        return "—"
    text = str(value)
    return text[:8]


def _bool(value: Any) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "—"


templates.env.filters["seconds"] = _seconds
templates.env.filters["number"] = _number
templates.env.filters["money"] = _money
templates.env.filters["date"] = _date
templates.env.filters["short_commit"] = _short_commit
templates.env.filters["yesno"] = _bool
templates.env.globals["github_url"] = GITHUB_URL

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def landing(request: Request):
    context = site_data.build_landing()
    return templates.TemplateResponse(request=request, name="landing.html", context=context)


@router.get("/demo", response_class=HTMLResponse)
def demo(request: Request):
    context = site_data.build_demo()
    return templates.TemplateResponse(request=request, name="demo.html", context=context)


@router.get("/live", response_class=HTMLResponse)
def live(request: Request):
    context = site_data.build_live()
    return templates.TemplateResponse(request=request, name="live.html", context=context)


@router.get("/api/demo/replay")
def demo_replay():
    return JSONResponse(
        content=site_data.replay_payload(),
        headers={"X-FlowScout-Evidence": "recorded-mock-run"},
    )
