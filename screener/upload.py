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

    proves it       success is confirmed by Naukri's own answer to the call
                    that attaches the file, or by the filename it shows back
                    changing. The toast is not trusted; it does not always
                    fire, and a silent no-op would otherwise read as a
                    successful update. See `landed`.

Headed by default, like `--extract`: Naukri fronts this page with Akamai, and
headless gets served "Access Denied".
"""
from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from . import selectors as S
from .paths import NAUKRI_RESUME_DIR, NAUKRI_STATE as DEFAULT_STATE, RESUME_DIR, UPLOAD_STATE
from .session import open_profile

log = logging.getLogger("screener.upload")

# How long to keep watching the card after Naukri confirms the attach, so the
# summary shows the new "Uploaded on" date rather than the one it replaced. The
# card took about 8 seconds to re-render on a live upload.
CARD_SETTLE_MS = 10_000


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


def attach_outcome(http_status: int, body: str | None) -> dict:
    """Naukri's reply to the attach call, reduced to whether it worked.

    The HTTP status and the body's own "status" flag both have to say yes. A
    live success reads `{"description": "Request completed successfully",
    "status": true}`.
    """
    try:
        data = json.loads(body or "")
    except ValueError:
        data = None
    flag = data.get("status") if isinstance(data, dict) else None
    detail = (data.get("description") if isinstance(data, dict) else None) or (body or "")[:200]
    return {"ok": http_status == 200 and flag is True, "http": http_status, "detail": detail}


def landed(before: dict, after: dict, attach: dict | None) -> bool:
    """Whether an upload reached the profile. Either proof is enough.

        filename       the name on the card changed - what recruiters see,
                       read straight off the page
        attach call    Naukri answered the POST that attaches the file with
                       success

    The attach call is the only proof when the same file goes up again on the
    same day, which is what a scheduled re-upload does: the name and the
    "Uploaded on" date on the card are already what they will be afterwards,
    so the card does not move at all.
    """
    renamed = bool(after.get("name")) and after.get("name") != before.get("name")
    return renamed or bool(attach and attach.get("ok"))


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


def _card(page) -> dict:
    """The resume card as it reads now: filename and "Uploaded on" date."""
    return {"name": _first_text(page, S.RESUME_NAME),
            "uploaded_on": _first_text(page, S.RESUME_UPLOADED_ON)}


def _attach(page, path: Path, timeout_sec: int = 90) -> dict:
    """Send `path` through the upload control on an open profile page, and prove it.

    Returns {"before", "after", "changed", "confirmed", "message"}: "changed" is
    the displayed filename moving, "confirmed" is Naukri's attach call answering
    success.
    """
    page.mouse.wheel(0, 2000)
    page.wait_for_timeout(1500)

    before = _card(page)
    log.info("Currently attached: %s (%s)", before["name"], before["uploaded_on"])

    file_input = _first_present(page, S.RESUME_FILE_INPUT)
    if file_input is None:
        raise UploadError(
            "Could not find the resume upload control on the profile page.\n"
            "  Naukri has probably reshipped the markup - fix "
            "RESUME_FILE_INPUT in screener/selectors.py."
        )

    attach: dict = {}

    def on_response(response):
        if (response.request.method == "POST"
                and urlparse(response.url).path.endswith(S.RESUME_ATTACH_ENDPOINT)):
            try:
                body = response.text()
            except Exception:
                body = ""
            attach.update(attach_outcome(response.status, body))

    page.on("response", on_response)
    file_input.set_input_files(str(path))
    log.info("Sent %s (%.0f KB)", path.name, path.stat().st_size / 1024)

    # Stop as soon as there is an answer: the filename changing, or the attach
    # call replying. After a reply, give the card a few seconds to re-render so
    # the summary shows its new date.
    deadline = timeout_sec * 1000
    waited, settle_until = 0, None
    after = dict(before)
    while waited < deadline:
        page.wait_for_timeout(1000)
        waited += 1000
        after = _card(page)
        if after["name"] and after["name"] != before["name"]:
            break
        if attach:
            if settle_until is None:
                settle_until = waited + CARD_SETTLE_MS
            if after != before or waited >= settle_until:
                break

    message = ""
    try:
        box = page.locator(S.RESUME_MSG_BOX).first
        if box.count():
            message = (box.inner_text(timeout=2000) or "").strip()
    except Exception:
        pass

    changed = bool(after["name"]) and after["name"] != before["name"]
    if not landed(before, after, attach):
        if attach:
            reason = (f"Naukri refused the upload (HTTP {attach['http']}: "
                      f"{attach['detail']}).\n")
        else:
            reason = (f"The attached resume is still '{after['name']}' after "
                      f"{timeout_sec}s, and Naukri never answered the attach "
                      f"call - the upload did not land.\n")
        raise UploadError(
            reason
            + (f"  Naukri said: {message}\n" if message else "")
            + "  Your existing resume has NOT been removed."
        )

    log.info("Now attached: %s (%s)%s", after["name"], after["uploaded_on"],
             "" if changed else " - same file, confirmed by Naukri")
    return {"before": before, "after": after, "changed": changed,
            "confirmed": bool(attach.get("ok")), "message": message}


def upload(path: Path, state_path: Path = DEFAULT_STATE, headless: bool = False,
           timeout_sec: int = 90) -> dict:
    """Attach `path` to the profile, replacing whatever is there. See `_attach`."""
    from playwright.sync_api import sync_playwright

    path = check(path)
    with sync_playwright() as p:
        browser, _context, page = open_profile(p, state_path, headless=headless)
        try:
            return _attach(page, path, timeout_sec)
        finally:
            browser.close()


def safe_filename(name: str | None) -> str:
    """A displayed resume name made safe to save on this machine.

    Only characters Windows refuses are replaced, and any folder part dropped, so
    an ordinary name - the case that matters, since it goes back up as the name
    recruiters see - comes through unchanged.
    """
    base = re.split(r"[\\/]", (name or "").strip())[-1]
    return re.sub(r'[<>:"|?*\x00-\x1f]', "_", base).rstrip(". ")


def download_current(page, dest_dir: Path = NAUKRI_RESUME_DIR) -> Path:
    """Save the resume attached to the profile, under the name the card shows.

    Naukri serves the download as "Resume.pdf" whatever the file is called, and
    re-uploading it under that name would change what recruiters see. So it is
    saved under the card's name instead. The bytes are Naukri's own copy -
    identical to the file that was uploaded, checked on a live download.

    The directory is emptied first. It holds only this one file, and a stale copy
    left beside a renamed resume must never be the one that goes back up.
    """
    page.mouse.wheel(0, 2000)
    page.wait_for_timeout(1500)
    shown = _card(page)["name"]
    name = safe_filename(shown)
    if not name:
        raise UploadError("No resume is attached to your Naukri profile, so there is "
                          "nothing to re-upload. Upload one: python main.py --upload-resume")
    if name != shown:
        log.warning("Saving %r as %r - the re-upload will show that name", shown, name)

    control = _first_present(page, S.RESUME_DOWNLOAD)
    if control is None:
        raise UploadError(
            "Could not find the download-resume control on the profile page.\n"
            "  Naukri has probably reshipped the markup - fix RESUME_DOWNLOAD in "
            "screener/selectors.py."
        )

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    for old in dest_dir.iterdir():
        if old.is_file():
            old.unlink()

    with page.expect_download(timeout=30_000) as info:
        try:
            control.click(timeout=5000)
        except Exception:
            control.dispatch_event("click")
    target = dest_dir / name
    info.value.save_as(str(target))
    log.info("Downloaded the attached resume as %s (%.0f KB)",
             target.name, target.stat().st_size / 1024)
    return check(target)


def reupload(state_path: Path = DEFAULT_STATE, headless: bool = False,
             timeout_sec: int = 90, dest_dir: Path = NAUKRI_RESUME_DIR,
             dry_run: bool = False) -> dict:
    """Upload the resume already on the profile again, in one browser session.

    Nothing local decides what goes up: the file comes off the profile first. If
    you change your resume on Naukri's own site, the next run re-uploads that one.
    With `dry_run`, the download happens and the upload does not.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser, _context, page = open_profile(p, state_path, headless=headless)
        try:
            path = download_current(page, dest_dir)
            if dry_run:
                return {"path": path, "before": _card(page)}
            result = _attach(page, path, timeout_sec)
            result["path"] = path
            return result
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
    lines = [
        "",
        f"  Was:  {before.get('name')}  ({before.get('uploaded_on')})",
        f"  Now:  {after.get('name')}  ({after.get('uploaded_on')})",
    ]
    if not result.get("changed") and result.get("confirmed"):
        lines.append("  Same file as before, so the card barely changes - Naukri "
                     "confirmed the upload.")
    lines += [
        "",
        "  Naukri shows this filename to recruiters, and the upload bumps your",
        "  profile's freshness date - which is what pushes you up their search.",
        "",
    ]
    return "\n".join(lines)


# --- last-run record, so a scheduled upload that fails is visible ------------

def load_record(path: Path = UPLOAD_STATE) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record(ok: bool, detail: str, path: Path = UPLOAD_STATE) -> None:
    """Note how the last real upload went. Dry runs are not recorded."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "last_run": datetime.now().isoformat(timespec="seconds"),
        "ok": ok,
        "detail": detail,
    }, indent=2), encoding="utf-8")


def status_line(path: Path = UPLOAD_STATE) -> str | None:
    """How the last upload went, for --check. None if it has never run."""
    data = load_record(path)
    if not data.get("last_run"):
        return None
    when = str(data["last_run"]).replace("T", " ")
    if data.get("ok"):
        return f"  Last resume upload: {when} - ok ({data.get('detail')})"
    first = (data.get("detail") or "unknown error").splitlines()[0]
    return (f"  Last resume upload: {when} - FAILED: {first}\n"
            "    If that is about the login: python main.py --login")
