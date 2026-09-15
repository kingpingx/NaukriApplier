"""Replace the resume attached to your Naukri profile.

This is the only module in the screener that *writes* to your account, and the
thing it writes is the document every recruiter who finds you will open. So it
is deliberately cautious in three ways:

    checks first    format and size are validated against Naukri's own stated
                    limits before a browser is even opened. A file it would
                    reject is reported here, where the message is readable,
                    rather than server-side where it is a toast that vanishes.

    never deletes   the widget has a delete-resume icon next to the upload one.
                    This code does not touch it. An upload that fails leaves
                    the old resume in place, which is strictly better than a
                    profile with no resume on it at all.

    proves it       success is confirmed by reading the filename Naukri shows
                    back and checking it changed. The toast is not trusted; it
                    does not always fire, and a silent no-op would otherwise
                    read as a successful update.

Headed by default, like `--extract`: Naukri fronts this page with Akamai, and
headless gets served "Access Denied".
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from . import selectors as S
from .paths import NAUKRI_STATE as DEFAULT_STATE, RESUME_DIR
from .session import open_profile

log = logging.getLogger("screener.upload")


class UploadError(RuntimeError):
    """The resume could not be uploaded."""


def _first_text(page, candidates: list[str]) -> str | None:
    for selector in candidates:
        try:
            node = page.locator(selector).first
            if node.count() and node.is_visible():
                text = (node.inner_text(timeout=3000) or "").strip()
                if text:
                    return text
        except Exception:
            continue
    return None


def _first_present(page, candidates: list[str]):
    for selector in candidates:
        node = page.locator(selector).first
        if node.count():
            return node
    return None


def check(path: Path) -> Path:
    """Validate a resume file against Naukri's limits. Returns the resolved path."""
    path = Path(path).expanduser().resolve()
    if not path.exists():
        raise UploadError(f"No such file: {path}")
    if not path.is_file():
        raise UploadError(f"Not a file: {path}")

    suffix = path.suffix.lower()
    if suffix not in S.RESUME_FORMATS:
        raise UploadError(
            f"Naukri does not accept '{suffix}' files.\n"
            f"  Supported: {', '.join(S.RESUME_FORMATS)}"
        )

    size = path.stat().st_size
    if size > S.RESUME_MAX_BYTES:
        raise UploadError(
            f"{path.name} is {size / 1024 / 1024:.1f} MB; Naukri's limit is 2 MB."
        )
    if size == 0:
        raise UploadError(f"{path.name} is empty.")
    return path


def current(state_path: Path = DEFAULT_STATE, headless: bool = False) -> dict:
    """What is attached to the profile right now, without changing anything."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser, _context, page = open_profile(p, state_path, headless=headless)
        try:
            page.mouse.wheel(0, 2000)
            page.wait_for_timeout(1500)
            return {
                "name": _first_text(page, S.RESUME_NAME),
                "uploaded_on": _first_text(page, S.RESUME_UPLOADED_ON),
            }
        finally:
            browser.close()


def upload(path: Path, state_path: Path = DEFAULT_STATE, headless: bool = False,
           timeout_sec: int = 90) -> dict:
    """Attach `path` to the profile, replacing whatever is there.

    Returns {"before": ..., "after": ..., "changed": bool}.
    """
    from playwright.sync_api import sync_playwright

    path = check(path)
    from playwright.sync_api import TimeoutError as PWTimeout

    with sync_playwright() as p:
        browser, _context, page = open_profile(p, state_path, headless=headless)
        try:
            page.mouse.wheel(0, 2000)
            page.wait_for_timeout(1500)

            before = {
                "name": _first_text(page, S.RESUME_NAME),
                "uploaded_on": _first_text(page, S.RESUME_UPLOADED_ON),
            }
            log.info("Currently attached: %s (%s)", before["name"], before["uploaded_on"])

            file_input = _first_present(page, S.RESUME_FILE_INPUT)
            if file_input is None:
                raise UploadError(
                    "Could not find the resume upload control on the profile page.\n"
                    "  Naukri has probably reshipped the markup - fix "
                    "RESUME_FILE_INPUT in screener/selectors.py."
                )

            file_input.set_input_files(str(path))
            log.info("Sent %s (%.0f KB)", path.name, path.stat().st_size / 1024)

            # Naukri re-renders the preview card once the upload lands. Waiting
            # on the *displayed filename* rather than a toast is what makes this
            # honest: if nothing changed, nothing was uploaded.
            deadline = timeout_sec * 1000
            waited = 0
            after = dict(before)
            while waited < deadline:
                page.wait_for_timeout(1000)
                waited += 1000
                after = {
                    "name": _first_text(page, S.RESUME_NAME),
                    "uploaded_on": _first_text(page, S.RESUME_UPLOADED_ON),
                }
                if after["name"] and after["name"] != before["name"]:
                    break

            message = ""
            try:
                box = page.locator(S.RESUME_MSG_BOX).first
                if box.count():
                    message = (box.inner_text(timeout=2000) or "").strip()
            except Exception:
                pass

            changed = bool(after["name"]) and after["name"] != before["name"]
            if not changed:
                raise UploadError(
                    f"The attached resume is still '{after['name']}' after "
                    f"{timeout_sec}s - the upload did not land.\n"
                    + (f"  Naukri said: {message}\n" if message else "")
                    + "  Your existing resume has NOT been removed."
                )

            log.info("Now attached: %s (%s)", after["name"], after["uploaded_on"])
            return {"before": before, "after": after, "changed": changed,
                    "message": message}
        finally:
            browser.close()


def staged_copy(source: Path, name: str | None = None) -> Path:
    """Copy a resume into data/resume/ under the name recruiters will see.

    Naukri displays the filename verbatim on the profile, so it is worth it
    being a name and not a scratch file - this is why the upload takes a path
    rather than always using whatever `find_resume()` happens to return.
    """
    source = Path(source).expanduser().resolve()
    RESUME_DIR.mkdir(parents=True, exist_ok=True)
    target = RESUME_DIR / (name or source.name)
    if target.resolve() != source:
        shutil.copy(source, target)
    return target


def summarise(result: dict) -> str:
    before, after = result["before"], result["after"]
    return "\n".join([
        "",
        f"  Was:  {before.get('name')}  ({before.get('uploaded_on')})",
        f"  Now:  {after.get('name')}  ({after.get('uploaded_on')})",
        "",
        "  Naukri shows this filename to recruiters, and the upload bumps your",
        "  profile's freshness date - which is what pushes you up their search.",
        "",
    ])
