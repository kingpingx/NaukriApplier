"""Every Naukri DOM selector lives here.

Naukri reships its profile page markup fairly often, so each field lists
several candidate selectors - the first one that resolves wins. When a field
stops extracting, fix it here and nowhere else.

Class names churn, but the visible section *headings* ("Resume headline",
"Key skills") are stable because recruiters and users read them. That is why
extraction falls back to heading-based scraping, which survives most redesigns.
"""
from __future__ import annotations

LOGIN_URL = "https://www.naukri.com/nlogin/login"
PROFILE_URL = "https://www.naukri.com/mnjuser/profile"
HOME_URL = "https://www.naukri.com/"

# URL fragments that only appear once a session is authenticated.
LOGGED_IN_URL_MARKERS = ("mnjuser/profile", "mnjuser/homepage", "mnjuser/recommendedjobs")

# Presence of any of these on the page means we are logged in.
LOGGED_IN_MARKERS = [
    ".view-profile-wrapper",
    ".nI-gNb-drawer__bars",
    "[data-ga-track*='My Naukri']",
    ".mn-hdr",
]

# Fields scraped from the profile page. Each entry maps to a list of candidate
# CSS selectors, tried in order.
#
# Verified against the live DOM on 2026-08-21. The page is assembled from
# "widget" cards: each section is a `#lazy*` root holding a `.widgetHead` (the
# title plus its edit/add affordances) and a `.widgetCont` (the content). These
# selectors point at the content and never the head, because the head carries
# icon-font ligature text - "editOneTheme", "locationOt" - that would otherwise
# be extracted as if it were profile data.
FIELDS: dict[str, list[str]] = {
    "name": [
        ".txt-wrapper .fullname",
        "span.fullname",
    ],
    "current_designation": [
        ".txt-wrapper .desg",
        ".subhdn .desg",
    ],
    # Header details are labelled only by their icon, so each one is addressed
    # through the icon beside it rather than by position in the list.
    "location": [
        ".detail-item:has(em.icon.loc) .txt",
    ],
    "experience": [
        ".detail-item:has(em.icon.exp) .txt",
    ],
    "current_salary": [
        ".detail-item:has(em.icon:text-is('walletOneTheme')) .txt",
    ],
    "notice_period": [
        ".detail-item:has(em.icon:text-is('calenderOneTheme')) .txt",
    ],
    "resume_headline": [
        "#lazyResumeHead .widgetCont",
    ],
    "profile_summary": [
        "#lazyProfileSummary .widgetCont",
    ],
    "career_profile": [
        "#lazyDesiredProfile .widgetCont",
    ],
    "personal_details": [
        "#lazyPersonalDetail .widgetCont",
    ],
    "accomplishments": [
        "#lazyAccomplishment .widgetCont",
    ],
    "resume_file": [
        "#lazyAttachCV .cvPreview",
        ".attachCV .cvPreview",
    ],
    "profile_completeness": [
        ".profile-strength .value",
        "[class*='completeness'] [class*='value']",
    ],
}

# Fields where we want every match, not just the first.
LIST_FIELDS: dict[str, list[str]] = {
    "key_skills": [
        "#lazyKeySkills .widgetCont .chip",
    ],
    "it_skills": [
        "#lazyITSkills .widgetCont li.collection",
    ],
    "employment": [
        "#lazyEmployment .row.emp-list",
    ],
    "education": [
        "#lazyEducation .row.edu-list",
    ],
    "projects": [
        "#lazyProject .row.project-list",
    ],
}

# Table header rows that match a list selector but carry no data.
LIST_NOISE = ("Skills Version Last used Experience",)

# Visible titles used by the widget fallback, keyed by output field name. Class
# names churn; the words recruiters read do not.
HEADING_FALLBACKS: dict[str, list[str]] = {
    "resume_headline": ["Resume headline"],
    "profile_summary": ["Profile summary"],
    "key_skills": ["Key skills"],
    "it_skills": ["IT skills"],
    "employment": ["Employment"],
    "education": ["Education"],
    "projects": ["Projects"],
    "accomplishments": ["Accomplishments"],
    "certifications": ["Certifications", "Certification"],
    "languages": ["Languages known", "Languages"],
    "career_profile": ["Career profile", "Desired job details"],
    "personal_details": ["Personal details"],
}

# Edit dialogs, keyed by the field they edit.
# `trigger` opens the dialog, `input` is the field to type into, `save` commits.
#
# Triggers are anchored to the section's visible heading and take the edit icon
# that is its *sibling*. The obvious `//*[text()='Key skills']/following::span`
# form looks equivalent but is not: the "Quick links" sidebar repeats every
# section name, so `following::` walks out of the nav and grabs whichever card's
# edit icon comes next in the document. Sibling-scoping excludes the nav,
# because nav links have no edit icon beside them.
#
# Verified against the live DOM on 2026-08-20.


def _edit_trigger(heading: str) -> str:
    return f"xpath=//*[normalize-space(text())='{heading}']/following-sibling::span[contains(@class,'edit')][1]"


EDITORS: dict[str, dict[str, list[str]]] = {
    "resume_headline": {
        "trigger": [_edit_trigger("Resume headline"), "#lazyResumeHead .edit.icon"],
        "input": ["#resumeHeadlineTxt", "textarea[name='resumeHeadline']"],
        # Note: btn-dark-ot, not btn-dark-ob. Only one Save is visible at a time.
        "save": ["#saveHeadline", "button.btn-dark-ot:visible", "button:has-text('Save'):visible"],
    },
    "profile_summary": {
        "trigger": [_edit_trigger("Profile summary"), "#lazyProfileSummary .edit.icon"],
        "input": ["#profileSummaryTxt", "textarea[name='profileSummary']"],
        "save": ["#saveSummary", "button.btn-dark-ot:visible", "button:has-text('Save'):visible"],
    },
    "key_skills": {
        "trigger": [_edit_trigger("Key skills"), "#lazyKeySkills .edit.icon"],
        "input": ["#keySkillSugg", "input[name='suggestor']"],
        "save": ["#saveKeySkills", "button.btn-dark-ot:visible", "button:has-text('Save'):visible"],
    },
}

# Naukri's own limits - exceeding them silently truncates or blocks the save.
MAX_LENGTHS = {"resume_headline": 250, "profile_summary": 1000}

# Key-skill chip widget. Typing text and pressing Enter does NOT create a chip;
# a suggestion has to be clicked. Anything left sitting in the input gets
# committed as a chip when Save is pressed, so the box must be cleared first.
SKILL_CHIP = ".chipsContainer .chip"
SKILL_CHIP_LABEL = ".tagTxt"
SKILL_CHIP_REMOVE = ".material-icons.close"
SKILL_SUGGESTIONS = "#sugDrp_keySkillSugg li.sugTouple"

# Toast / confirmation that a save actually landed.
SAVE_CONFIRMATIONS = [
    ".nI-gNb-info",
    "text=/successfully/i",
    "text=/updated/i",
]

# Section names that appear verbatim in the "Quick links" sidebar. Heading-based
# extraction uses these to recognise when it has climbed out of a profile card
# and into the nav: three or more of these in one block means it is the sidebar,
# not a section. See _HEADING_JS in extract.py.
SECTION_LABELS = [
    "Quick links",
    "Resume headline",
    "Key skills",
    "IT skills",
    "Employment",
    "Education",
    "Projects",
    "Profile summary",
    "Accomplishments",
    "Career profile",
    "Personal details",
]


# --------------------------------------------------------------- job listings
#
# Verified against a live job-detail page on 2026-08-21. Naukri ships hashed
# CSS module class names here ("styles_apply-button__uJI3A"), so the stable
# hooks are the id and the unhashed companion class, not the module class.

JOB_APPLY_BUTTON = [
    "#apply-button",
    "button.apply-button",
    "button[class*='apply-button']",
]

# The button's own label is the most reliable signal of what a click will do:
# an off-site posting reads "Apply on company site".
APPLY_OFFSITE_LABELS = ("company site", "company website", "apply on company")
APPLY_DONE_LABELS = ("applied", "already applied")

# The definitive confirmation. Once an application lands, the job page itself
# replaces the Apply button with this badge. It is far more reliable than the
# toast: a questionnaire that completes may show no toast at all, which is how
# a successful application first got recorded as a failure.
APPLY_APPLIED_MARKERS = [
    "[class*='already-applied' i]",
    "span.already-applied",
]

# Shown once an application actually lands.
APPLY_SUCCESS = [
    "text=/successfully applied/i",
    "text=/application sent/i",
    "text=/you have applied/i",
    ".apply-message",
]

# The recruiter questionnaire ("chatbot"). Verified against a live drawer on
# 2026-08-21. Its presence means the application is NOT yet submitted - it is
# waiting on answers.
#
# Every id in this widget is randomised per session (_i7qo2hnh6Drawer one run,
# _7k4bm8lhuDrawer the next), so only class names are usable here.
JOB_CHATBOT = [
    ".chatbot_Drawer",
    ".chatbot_DrawerContentWrapper",
    "[class*='chatBotContainer' i]",
]

JOB_CHATBOT_CLOSE = [
    ".chatbot_Nav .crossIcon",
    ".crossIcon.chatBot",
    "[class*='chatbot' i] [class*='cross' i]",
]

# The bot's messages. The last one is the question currently being asked.
CHATBOT_QUESTION = "li.botItem .botMsg"

# Single-select questions render as radios whose <label> carries the option
# text. The inputs' ids contain spaces ("2 months"), so click the label.
CHATBOT_RADIO_LABEL = "label.ssrc__label"

# Free-text answers go into a contenteditable div, not an <input>. Its wrapper
# carries `d-none` while a choice-based question is on screen.
CHATBOT_TEXT_INPUT = ".chatbot_InputContainer .textArea[contenteditable='true']"
CHATBOT_INPUT_WRAPPER = ".chatbot_SendMessageContainer"

# "Save" is a styled div, not a <button>, and its parent carries `disabled`
# until the current question has an answer.
CHATBOT_SAVE = ".sendMsgbtn_container .sendMsg"
CHATBOT_SAVE_DISABLED = ".sendMsgbtn_container .send.disabled"

# The bot's closing message once every question has been answered.
CHATBOT_SUCCESS = [
    "text=/successfully applied/i",
    "text=/application (has been )?(sent|submitted)/i",
    "text=/thank you for applying/i",
]
