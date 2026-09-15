#!/usr/bin/env python
"""Naukri job screener - find the jobs that actually match you.

    python main.py --setup              create config.yaml and show what's next
    python main.py --resume my_cv.pdf   parse your resume into facts
    python main.py --login              sign in to Naukri once, save the session
    python main.py --extract            read your live Naukri profile (optional)
    python main.py --upload-resume      replace the resume on your Naukri profile
    python main.py --match JOB_URL      check one opening; fix your profile to match
    python main.py --schedule TIMES     scan daily at those times, e.g. 09:00,18:00
    python main.py --scan               search, score, rank, write the results
    python main.py --check              show the config without touching a browser
    python main.py --roles              list role packs, and which fits your resume
    python main.py --new-role NAME      scaffold a pack for a field with none

Start with --setup. It tells you which of the rest you need.
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

from screener import config as config_mod
from screener import notify as notify_mod
from screener import paths, resume as resume_mod, scan as scan_mod, upload as upload_mod

EXAMPLE_CONFIG = paths.ROOT / "config.example.yaml"

# Windows consoles default to cp1252, and salary fields carry a rupee sign -
# printing a summary would raise UnicodeEncodeError and lose an otherwise
# successful run. Degrade unprintable characters instead.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def _setup_logging(verbose: bool) -> None:
    paths.LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(paths.LOG_DIR / "screener.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def cmd_setup() -> int:
    paths.ensure()
    target = paths.CONFIG_PATH
    if target.exists():
        print(f"\n  {target} already exists - leaving it alone.\n")
    else:
        shutil.copy(EXAMPLE_CONFIG, target)
        print(f"\n  Created {target}")

    roles = config_mod.available_roles()
    print(f"""
  Next, in order:

    1. Put your resume in
         {paths.RESUME_DIR}
       then run:  python main.py --resume

    2. Pick a role pack. Available:
         {', '.join(roles) if roles else '(none)'}
       Set `role:` in config.yaml. Skip it and the screener works off your
       resume alone, just less precisely.

    3. Sign in once:
         python main.py --login

    4. Check what it will do, without opening a browser:
         python main.py --check

    5. Run it:
         python main.py --scan
""")
    return 0


def cmd_roles() -> int:
    roles = config_mod.available_roles()
    if not roles:
        print(f"\n  No role packs found in {paths.PROFILES_DIR}\n")
        return 1

    # If a resume has been parsed, say which pack it matches - that is the
    # question someone running --roles is actually asking.
    facts = resume_mod.load()
    detected, scores = config_mod.detect_role(facts) if facts else (None, {})

    print(f"\n  Role packs in {paths.PROFILES_DIR}:\n")
    for role in roles:
        try:
            pack = config_mod.load_pack(role)
        except config_mod.ConfigError:
            continue
        description = pack.get("description") or ""
        mark = " <- matches your resume" if role == detected else ""
        print(f"    {role:<24} {description}{mark}")

    if facts and not detected:
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:2]
        print("\n  None of these clearly matches your resume"
              + (f" (closest: {', '.join(r for r, _ in ranked)})." if ranked else ".")
              + "\n  Either pick one anyway, or make your own:"
              + "\n      python main.py --new-role \"Android Developer\"")
    elif not facts:
        print("\n  Run `python main.py --resume <cv>` and this will tell you which one fits.")

    print("\n  Set one as `role:` in config.yaml.\n")
    return 0


def cmd_new_role(name: str) -> int:
    facts = resume_mod.load()
    path = config_mod.scaffold_pack(name, facts)
    seeded = "seeded from your resume" if facts else "empty - fill in the lists"
    print(f"""
  Created {path}  ({seeded})

  Open it and check three lists:

    searches        job TITLES as postings word them, not skills.
                    "Java Developer", not "Hibernate".
    must_have_any   the "is this my field" gate. Too narrow and you see
                    nothing; too broad and the neighbouring field leaks in.
    vocabulary      every tool and term in your field. This is also what
                    role detection matches against, so length helps.

  Then set it in config.yaml:

      role: {path.stem}

  Check it before running a scan:

      python main.py --check

  If it works, a pull request adding it helps the next person in your field.
""")
    return 0


def cmd_resume(path_arg: str | None) -> int:
    paths.ensure()
    source = Path(path_arg) if path_arg else None
    facts = resume_mod.parse(source, vocabulary=_pack_vocabulary())

    # A resume dropped in from elsewhere is copied in, so later runs find it
    # without the user having to remember the path they typed once.
    if source and source.exists() and source.parent.resolve() != paths.RESUME_DIR.resolve():
        paths.RESUME_DIR.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy(source, paths.RESUME_DIR / source.name)
        except OSError as exc:
            logging.getLogger("screener").debug("Could not copy the resume in: %s", exc)

    resume_mod.save(facts)
    print("\n" + resume_mod.summarise(facts) + "\n")
    return 0


def cmd_upload_resume(path_arg, as_name: str | None, dry_run: bool, headless: bool) -> int:
    """Replace the resume on the live Naukri profile."""
    paths.ensure()
    source = Path(path_arg) if path_arg is not True else resume_mod.find_resume()
    if source is None:
        print(f"\n  No resume found in {paths.RESUME_DIR}."
              f"\n  Pass one:  python main.py --upload-resume path/to/cv.pdf\n")
        return 2

    staged = upload_mod.staged_copy(source, as_name)
    upload_mod.check(staged)
    size_kb = staged.stat().st_size / 1024

    if dry_run:
        live = upload_mod.current(headless=headless)
        print(f"""
  Would upload:  {staged}  ({size_kb:.0f} KB)
  Replacing:     {live.get('name')}  ({live.get('uploaded_on')})

  Nothing was changed. Drop --dry-run to do it.
""")
        return 0

    print(f"\n  Uploading {staged.name} ({size_kb:.0f} KB) to your Naukri profile...")
    result = upload_mod.upload(staged, headless=headless)
    print(upload_mod.summarise(result))
    return 0


def cmd_match(url: str, cv: str | None, as_name: str | None, headless: bool) -> int:
    """Check one opening against a resume, then offer the two profile fixes."""
    from screener import edit as edit_mod, match as match_mod
    paths.ensure()
    config = config_mod.load(resume_facts=_match_facts(cv))
    details, naukri = match_mod.fetch(url, headless=headless)
    result = match_mod.compare(details, naukri, config)
    print(match_mod.summarise(result, config))

    # Two writes to a recruiter-facing profile, each asked for separately.
    if result["fixable"] and _confirm(
            f"Add {', '.join(result['fixable'])} to your Naukri key skills?"):
        print(edit_mod.summarise(edit_mod.apply(add_skills=result["fixable"], headless=headless)))
    if cv and _confirm(f"Replace the resume on your Naukri profile with "
                       f"{as_name or Path(cv).name}?"):
        staged = upload_mod.staged_copy(Path(cv), as_name)
        print(upload_mod.summarise(upload_mod.upload(staged, headless=headless)))
    return 0


def _match_facts(cv: str | None) -> dict:
    """The resume to check: a file given for this run, else the parsed one."""
    if cv:
        # Parsed, not saved - checking a variant must not overwrite data/resume.json.
        return resume_mod.parse(Path(cv), vocabulary=_pack_vocabulary())
    facts = resume_mod.load()
    if not facts:
        raise resume_mod.ResumeError(
            "No parsed resume yet. Run --resume first, or pass --cv <file>.")
    return facts


def _confirm(question: str) -> bool:
    """Ask before a write to the live profile. Anything but y/yes is a no."""
    try:
        return input(f"\n  {question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _config_value(key: str):
    """Read one key out of config.yaml, tolerating an otherwise broken file."""
    if not paths.CONFIG_PATH.exists():
        return None
    try:
        import yaml
        loaded = yaml.safe_load(paths.CONFIG_PATH.read_text(encoding="utf-8")) or {}
        return loaded.get(key) if isinstance(loaded, dict) else None
    except Exception:
        return None


def _pack_vocabulary() -> list[str]:
    """The pinned role pack's vocabulary, which sharpens a resume parse."""
    try:
        # A config that cannot load yet (no facts on disk - which is exactly the
        # case on a first run) must not block parsing the resume that fixes that.
        return list(config_mod.load_pack(_config_value("role")).get("vocabulary") or [])
    except config_mod.ConfigError as exc:
        logging.getLogger("screener").debug("No role pack for the parse: %s", exc)
        return []


def _check_boards(config: dict) -> None:
    """The config.yaml checks, re-run for board choices made on the command line."""
    from screener.sources import boards as boards_mod
    problems = boards_mod.unknown(config.get("boards"))
    if problems:
        raise config_mod.ConfigError("Unknown boards:\n  " + "\n  ".join(problems))
    if config.get("source") == "none" and not boards_mod.selected(config):
        raise config_mod.ConfigError(
            "--source none reads no Naukri, and no boards are selected. Add --boards all.")


def cmd_check() -> int:
    config = config_mod.load()
    print("\n" + config_mod.summarise(config) + "\n")
    missing = []
    if config.get("source") != "none" and not paths.NAUKRI_STATE.exists():
        missing.append("  No saved Naukri session. Run: python main.py --login")
    if config.get("profile_years") is None:
        missing.append("  Experience unknown - experience scoring will sit mid-band.\n"
                       "    Set `years:` in config.yaml.")
    if missing:
        print("\n".join(missing) + "\n")
    return 0


def cmd_scan(args) -> int:
    config = config_mod.load()
    if args.source:
        config["source"] = args.source
    if args.boards is not None:
        config["boards"] = [b.strip() for b in args.boards.split(",") if b.strip()]
    if args.source or args.boards is not None:
        _check_boards(config)
    if args.posted_days is not None:
        config["posted_within_days"] = args.posted_days
    config["headless"] = args.headless

    print("\n" + config_mod.summarise(config) + "\n")
    if args.notify:
        notify_mod.send("Naukri scan started",
                        f"{len(config.get('searches') or [])} searches - results in a minute or two")

    summary = scan_mod.run(config, refresh=args.refresh, limit=args.limit)
    written = scan_mod.write(summary, config, excel=not args.no_excel, html=not args.no_html)
    print(scan_mod.summarise(summary, written))
    if args.notify:
        notify_mod.send(*scan_mod.headline(summary, written),
                        critical=bool(summary.get("failed_searches")))
    return 0


def cmd_schedule(times_text: str) -> int:
    from screener import schedule as schedule_mod
    times = schedule_mod.parse_times(times_text)
    upcoming = schedule_mod.install(times)
    print(f"\n  Scans scheduled daily at {', '.join(times)}.\n")
    print("\n".join("  " + line for line in upcoming.strip().splitlines()))
    print("\n  Each scan pops up a notification when it starts, finishes or fails.\n"
          "  They run only while you are logged in - the browser needs your display.\n"
          "  Change the times by running --schedule again; remove with --unschedule.\n")
    return 0


def cmd_unschedule() -> int:
    from screener import schedule as schedule_mod
    print("\n  Scheduled scans removed.\n" if schedule_mod.remove()
          else "\n  No scheduled scans were installed.\n")
    return 0


def _expected_errors() -> tuple[type[Exception], ...]:
    """Failures whose message is written for you, so they print without a trace.

    Imported lazily, as before: the sources package pulls in Playwright, which
    --check and --roles should not pay for.
    """
    from screener.edit import EditError
    from screener.match import MatchError
    from screener.schedule import ScheduleError
    from screener.session import NotLoggedIn
    from screener.sources import SourceError
    return (upload_mod.UploadError, resume_mod.ResumeError, config_mod.ConfigError,
            NotLoggedIn, SourceError, MatchError, EditError, ScheduleError)


def _fail(message: str, code: int, notify: bool) -> int:
    """Print a failure - and with --notify pop it up, since nobody is watching."""
    print(f"\n  {message}\n")
    if notify:
        notify_mod.send("Naukri scan failed", message, critical=True)
    return code


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--setup", action="store_true", help="Create config.yaml and print the next steps")
    action.add_argument("--roles", action="store_true", help="List the role packs and say which fits your resume")
    action.add_argument("--new-role", dest="new_role", metavar="NAME",
                        help="Scaffold profiles/<name>.yaml for a field with no pack")
    action.add_argument("--resume", nargs="?", const=True, metavar="PATH",
                        help="Parse a resume into data/resume.json")
    action.add_argument("--login", action="store_true", help="Sign in to Naukri and save the session")
    action.add_argument("--extract", action="store_true", help="Read your live Naukri profile into data/")
    action.add_argument("--upload-resume", dest="upload_resume", nargs="?", const=True,
                        metavar="PATH",
                        help="Replace the resume attached to your Naukri profile")
    action.add_argument("--match", metavar="JOB_URL",
                        help="Check one Naukri opening against your resume, then offer to fix your profile")
    parser.add_argument("--cv", metavar="PATH",
                        help="With --match: check this resume file instead of data/resume.json")
    parser.add_argument("--as-name", dest="as_name", metavar="FILENAME",
                        help="Upload under this filename - recruiters see it")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run",
                        help="With --upload-resume: show what would change, upload nothing")
    action.add_argument("--check", action="store_true", help="Print the effective config and exit")
    action.add_argument("--scan", action="store_true", help="Search, score and rank today's jobs")
    action.add_argument("--schedule", metavar="TIMES",
                        help="Scan daily at these times, e.g. 09:00,13:00,18:00 (systemd timer)")
    action.add_argument("--unschedule", action="store_true", help="Remove the scheduled scans")
    parser.add_argument("--notify", action="store_true",
                        help="With --scan: desktop pop-up when it starts, finishes or fails")

    parser.add_argument("--source", choices=("local", "apify", "none"),
                        help="Override `source:` for this run; 'none' skips Naukri")
    parser.add_argument("--boards", metavar="LIST",
                        help="Remote boards to read too, comma-separated, e.g. all or "
                             "remoteok,weworkremotely,hn - overrides `boards:`")
    parser.add_argument("--limit", type=int, metavar="N", help="Keep at most N scored jobs")
    parser.add_argument("--posted-days", type=float, default=None, dest="posted_days", metavar="N",
                        help="Only listings posted in the last N days")
    parser.add_argument("--refresh", action="store_true",
                        help="Re-score jobs already in the ledger (use after editing config.yaml)")
    parser.add_argument("--headless", action="store_true",
                        help="Minimize the browser window (every command but --login). "
                             "`headless: true` in config.yaml makes this the default.")
    parser.add_argument("--no-excel", action="store_true", dest="no_excel", help="Skip the .xlsx")
    parser.add_argument("--no-html", action="store_true", dest="no_html", help="Skip the HTML page")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()
    # Read here rather than per command: --upload-resume and --extract never
    # load the full config, and `headless: true` has to reach them too.
    args.headless = args.headless or bool(_config_value("headless"))

    _setup_logging(args.verbose)

    try:
        if args.setup:
            return cmd_setup()
        if args.roles:
            return cmd_roles()
        if args.new_role:
            return cmd_new_role(args.new_role)
        if args.resume:
            return cmd_resume(None if args.resume is True else args.resume)
        if args.check:
            return cmd_check()
        if args.scan:
            return cmd_scan(args)
        if args.schedule:
            return cmd_schedule(args.schedule)
        if args.unschedule:
            return cmd_unschedule()

        if args.login:
            from screener import session as session_mod
            paths.ensure()
            return 0 if session_mod.login() else 1

        if args.match:
            return cmd_match(args.match, args.cv, args.as_name, args.headless)

        if args.upload_resume:
            return cmd_upload_resume(args.upload_resume, args.as_name, args.dry_run, args.headless)

        if args.extract:
            from screener import extract as extract_mod
            paths.ensure()
            profile = extract_mod.extract(headless=args.headless)
            print(extract_mod.summarise(profile))
            return 0

    except KeyboardInterrupt:
        print("\n  Stopped.\n")
        return 130
    except Exception as exc:
        if isinstance(exc, _expected_errors()):
            return _fail(str(exc), 2, args.notify)
        logging.getLogger("screener").exception("Unhandled error")
        return _fail(f"Error: {exc}\n  See logs/screener.log", 1, args.notify)

    return 0


if __name__ == "__main__":
    sys.exit(main())
