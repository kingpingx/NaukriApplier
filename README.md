# Naukri job screener

Naukri returns hundreds of listings for any search worth running, ranked by
Naukri's idea of relevance. This reads them against *your* resume, scores each
one 0–100, and hands you a ranked shortlist with the reasoning attached.

It finds jobs. It does not apply to them — see [Where the line is](#where-the-line-is).

```
  Collected 187 listings via local.
    41 already seen, 96 rejected outright.

  Shortlist:    12
  Worth a read: 23

     84.3  Senior .NET Developer                         Acme Analytics
     79.1  Full Stack Developer (.NET / Angular)         Globex
     76.8  Lead ASP.NET Core Developer                   Initech
```

Every score comes with its breakdown, so when it ranks something you would not
have, you can see why and fix the config instead of guessing:

```
skills=38.2 title=22 experience=15 location=10 freshness=3 | matched: C#, ASP.NET Core, Angular, SQL Server
```

## Quick start

Five commands. **You do not need to configure anything** — it reads your resume
and works out the rest.

```bash
git clone https://github.com/<you>/naukri-job-screener
cd naukri-job-screener
pip install -r requirements.txt
python -m playwright install chromium

python main.py --setup                    # creates config.yaml
python main.py --resume path/to/cv.pdf    # parse your resume
python main.py --login                    # sign in once, session is saved
python main.py --check                    # see what it will search for
python main.py --scan                     # go
```

Results land in `data/jobs/`: a ranked `.xlsx`, a browsable HTML page, a
markdown report, and the raw JSON.

### It works out which field you're in

`--check` before you have touched a single setting:

```
  Role pack:  dotnet-fullstack  (matched to your resume - set `role:` to pin it)
  Experience: 4 years
  Skills:     15
  Locations:  Hyderabad, Remote
  Searches:
                - Dot Net Developer in Hyderabad
                - .NET Developer in Hyderabad
                - Full Stack Developer in Hyderabad
                - ASP.NET Core Developer in Hyderabad
                ...
```

Your resume is matched against every role pack on three signals — your job
titles, the tools you list, and the language of the resume body. If one wins
clearly, it is used. **If nothing wins clearly, it says so rather than
guessing** — a wrong field is worse than no field.

Same repo, same commands, a different resume:

| Resume | Detected | First search |
| --- | --- | --- |
| C# / ASP.NET Core / Angular, Mumbai | `dotnet-fullstack` | Dot Net Developer in Mumbai |
| Java / Spring Boot, Hyderabad | `backend-engineer` | Backend Developer in Hyderabad |
| React / Node / Mongo, Pune | `fullstack-developer` | Full Stack Developer in Pune |
| Spark / Airflow / dbt, Bengaluru | `data-engineer` | Data Engineer in Bengaluru |

## How it decides

Five components, weighted, normalised to 100:

| | out of | what it measures |
| --- | ---: | --- |
| skills | 45 | how much of what the job asks for you actually have |
| title | 25 | is this the kind of role you want |
| experience | 15 | are you inside the band they asked for |
| location | 10 | is it somewhere you would work |
| freshness | 5 | recent postings get replies; month-old ones do not |

Before any of that, **hard rejects** drop a job outright — wrong field, an
excluded company, a title you never want, an experience gap too wide to be
worth reading. Those are the ones that make a shortlist short.

Reweight it for your situation. Someone who will relocate anywhere:

```yaml
weights:
  skills: 55
  title: 30
  experience: 15
  location: 0      # anywhere is fine
  freshness: 5
```

The split renormalises to 100 whatever you put in, so the thresholds in
`config.yaml` keep meaning the same thing.

## Role packs

A role pack is the vocabulary for one job family — the terms that mean the same
thing, the words that say "this is my field", and the job titles worth searching
for. Detection picks one for you; this is how to see, change or add them.

```bash
python main.py --roles
```

```
  backend-engineer      Backend, server-side and API engineering
  data-engineer         Data engineering, ETL pipelines and data platforms
  dotnet-fullstack      .NET full stack - C# / ASP.NET Core back end with an Angular front end  <- matches your resume
  frontend-engineer     Frontend, UI and web application engineering
  fullstack-developer   Full stack web development, front and back of the same app
```

Run it after `--resume` and it marks the one that matches you.

To pin it — worth doing once you know which you want, so a resume edit cannot
change it under you:

```yaml
role: dotnet-fullstack
```

### Your field isn't listed

Scaffold one. It seeds from your own resume, so the first version is already
half right:

```bash
python main.py --new-role "Android Developer"
```

```yaml
# profiles/android-developer.yaml  - generated, then edit
searches:
  - Android Developer
  - Kotlin Developer
  - Jetpack Compose Developer
must_have_any:
  - android developer
vocabulary:
  - Kotlin
  - Jetpack Compose
  - Retrofit
  ...
```

Three lists matter:

| | what it does | getting it wrong |
| --- | --- | --- |
| `searches` | job **titles**, as postings word them | `Hibernate` returns noise; `Java Developer` returns jobs |
| `must_have_any` | the "is this my field" gate | too narrow → nothing; too broad → the next field leaks in |
| `vocabulary` | terms recognised in a description | short list → weaker scoring *and* weaker detection |

Then `python main.py --check` to see the searches it produces. A pack you add is
immediately detectable, so the next person with a resume like yours gets matched
to it automatically.

It's YAML — no Python. **A pull request adding a pack is the most useful thing
you can contribute here.**

### Or skip packs entirely

Leave `role: null` with no pack matching and the screener still runs off your
resume alone. It pairs your skills with a role noun so the searches stay
sensible — `Java Developer`, never a bare `Hibernate`. It works; it is just
less precise, because there's no field gate to reject the neighbouring field
and no vocabulary to catch a skill your CV never spelled out.

## Configuration

`config.yaml` is yours and gitignored. Every key is optional; anything absent is
derived from your resume.

```yaml
role: dotnet-fullstack
years: 7                      # only if your resume doesn't state it plainly

searches:                     # leave empty to derive from your role + title
  - keyword: .NET Developer
    location: Pune
  - keyword: ASP.NET Core Developer
    location: null            # null = all of India
    pages: 2

preferred_locations: [Pune, Remote, Hybrid]

exclude_title_keywords: [intern, fresher, trainee]
exclude_description_keywords: ["service bond", unpaid]
exclude_companies: []

min_salary_lpa: 20            # only drops jobs that state a salary
posted_within_days: 7

auto_apply_min_score: 72      # shortlist at or above
review_min_score: 55          # worth a read at or above
```

See [config.example.yaml](config.example.yaml) for every key with its comment.

## Resume parsing

```bash
python main.py --resume my_cv.pdf      # or .docx, .txt, .md
```

Reads skills, total years, per-skill years, job titles and location into
`data/resume.json`.

It is a **parser, not an LLM** — deliberately. A parser you can read is one you
can correct, and every fact it extracts is written to a JSON file you can edit
by hand before a scan uses it. Anything it gets wrong, you fix once.

It prints what it found, so check it:

```
  Experience: 7 years
  Location:   Bengaluru
  Skills:     15 found
              C#, ASP.NET Core, Entity Framework Core, Angular, SQL Server...
  Titles:     Senior .NET Developer
```

Contact details are stripped before any matching happens — a phone number in a
profile blob would otherwise match against job descriptions as if it were a
skill.

**Scanned-image PDFs will not work.** There is no OCR here. Export a text-based
copy, or save as `.txt`.

### Optionally: your live Naukri profile

```bash
python main.py --extract
```

Reads your Naukri profile into `data/profile.json` and merges it with the
resume. Worth doing for one reason: Naukri's IT-skills table carries per-skill
durations it maintains itself, which beat a number typed into a CV years ago.
Where the two disagree, the profile wins.

### Optionally: push your resume back to Naukri

```bash
python main.py --upload-resume --dry-run          # show what would change
python main.py --upload-resume                    # newest file in data/resume/
python main.py --upload-resume cv.pdf --as-name "Your Name Resume.pdf"
```

The one command here that *writes* to your account. It replaces the attached
resume and nothing else - it never touches the delete-resume control, so a
failed upload leaves the old file in place rather than leaving you with none.

Success is confirmed by reading back the filename Naukri displays and checking
it changed, because the toast does not always fire and a silent no-op would
otherwise look like a successful update. Format and size are checked against
Naukri's own limits (doc/docx/rtf/pdf, 2 MB) before the browser opens.

Two things worth knowing before you run it: the filename is shown to recruiters,
which is what `--as-name` is for, and the upload bumps your profile's freshness
date - normally what you want, since recency drives recruiter search ranking.

### Check one opening before you apply

```bash
python main.py --match "https://www.naukri.com/job-listings-...-123456789012"
python main.py --match "<job url>" --cv tailored_cv.pdf    # check a different CV
```

Two verdicts for that one job:

- **Yours** - the CV scored exactly as `--scan` would, with the job's key skills
  split into what you have and what you lack, its must-haves starred.
- **Naukri's** - the site's own match check against your *live profile*. It
  reads your key-skill chips, not the resume file, and it is what a recruiter's
  filtered search sees.

Where they disagree - your CV has C#, Naukri says you lack it - your profile is
stale, and it offers to add those skills to your Naukri key skills. Where both
say a skill is missing it is reported as a gap and never added: claiming a
skill you don't have is the same line as answering screening questions for you.

With `--cv`, it then offers to replace the resume on your profile with that
file. Both writes ask first and default to no, and adding key skills never
removes an existing one. A `--cv` file is only parsed for the check -
`data/resume.json` is left as it is.

## Run it on a schedule

```bash
python main.py --schedule 09:00,13:00,18:00    # any times, 24-hour
python main.py --unschedule
```

On Linux this installs a systemd user timer that runs `--scan --notify` at those
times. `--notify` pops up a desktop notification when a scan starts, when it
finishes (with the top matches), and when it fails - an expired login says so
instead of quietly finding nothing.

- It runs only while you are logged in: the browser needs your display, even
  minimized.
- A scan that fell due while the laptop slept runs when it wakes.
- Each scan keeps its own files - `openings-<date>-r1.html`, `-r2`, ... - and
  every page links to the day's other scans. Later scans the same day list only
  jobs the earlier ones had not shown you.
- Output of scheduled runs: `journalctl --user -u naukri-scan`.

On Windows or macOS no timer is installed for you: point Task Scheduler or
launchd at `python main.py --scan --notify` in the repo folder, for a logged-in
session.

## Where jobs come from

```yaml
source: local     # default
```

**`local`** — Playwright on your machine with your saved session. Works after a
`pip install`. This is the one to use. Add `--headless` to any command except
`--login` to minimize the browser window, or set `headless: true` in
`config.yaml` to make that the default.

**`apify`** — the same navigation, in an Apify actor in the cloud. The only real
advantage is that a scheduled run happens whether or not your laptop is open.
It costs money, and it means uploading your Naukri session to Apify's
infrastructure.

Before enabling it, read [docs/apify.md](docs/apify.md) — it is candid about
three constraints that catch people out: the search endpoints are request-signed
so the actor cannot be a cheap HTTP scraper, it has to run headed under Xvfb
because Akamai blocks headless Chromium, and datacenter proxies do not work.

## Where the line is

**This tool does not apply to jobs.** It finds them, ranks them, and stops.

That is deliberate. Automating the application means an unattended process
answering a recruiter's screening questions — notice period, expected salary,
willingness to relocate — in your name. Those answers are representations you
are making to an employer, and a script guessing them is a script lying on your
behalf. No ranking is worth that.

So the output is a shortlist with links. You click, you read, you answer. The
tedious part — reading 187 listings to find the 12 worth your time — is the part
worth automating, and it is the part this does.

## Your data

Everything personal stays on your machine, under `data/`, which is gitignored.

`data/state.json` and `data/linkedin_state.json` **are your logins** — anyone
holding one is signed in as you, no password or OTP needed. Treat them like
passwords. They are the first entries in `.gitignore` for that reason.

Nothing is sent anywhere except Naukri itself, unless you explicitly turn on the
Apify source. There is no telemetry and no server.

To keep your data outside the clone entirely:

```bash
export SCREENER_HOME=~/.naukri-screener      # or $env:SCREENER_HOME on Windows
```

## Troubleshooting

**"Access Denied" / every search returns nothing**
Naukri sits behind Akamai, which blocks every true headless mode. `--headless`
here does not use one: it runs a normal browser and minimizes its window,
which Naukri allows. If it persists, your session has probably expired — re-run
`--login`.

**"Saved session has expired"**
Sessions last a few weeks. `python main.py --login` again.

**Nothing clears the thresholds**
Run `python main.py --check` and read the searches. If they look wrong, your
resume parsed badly — check `data/resume.json`. If they look right, lower
`review_min_score` and re-run with `--refresh`.

**Detected the wrong field, or none at all**
`python main.py --roles` shows the ranking. Set `role:` explicitly to override
it — your choice always wins over detection. If nothing fits, scaffold your own
with `--new-role`.

**The searches look nothing like my field**
Almost always a bad resume parse. Check `data/resume.json` — if `titles` and
`skills` are wrong there, everything downstream is. Fix that file by hand, or
set `searches:` in `config.yaml` explicitly and skip derivation entirely.

**`--scan` finds nothing new every day**
Expected — the ledger hides jobs it has already shown you. `--refresh`
re-scores everything, which is what you want after editing your config.

**Resume parsed badly**
Edit `data/resume.json` by hand. It is read as-is on the next scan and nothing
overwrites it until you re-run `--resume`.

## Development

```bash
python -m pytest tests/ -q
```

110 tests, no network. They cover the parts that quietly go wrong: role
detection (every shipped pack, plus the resumes it should refuse to classify),
scoring (a bug here ranks the wrong jobs and looks like it worked), search
derivation, resume parsing, config layering, and a check that no personal data
has crept into a tracked file.

## Contributing

Most useful contribution: **a role pack for a field that has none.** Copy the
closest `profiles/*.yaml`, rewrite the word lists, open a PR. No Python needed.

Also welcome: selector fixes when Naukri reships its markup, and parser
improvements for resume layouts that trip it up (attach a redacted sample).

## Licence

MIT — see [LICENSE](LICENSE).

Not affiliated with or endorsed by Naukri.com or Info Edge. It drives the site
in a normal browser, at the rate a person reads results, using your own account.
You are responsible for staying within Naukri's terms of service.
