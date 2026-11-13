"""
=============================================================
SERFF FILING ACCESS AUTOMATION  (class based, two phases)
=============================================================

Rules implemented (locked with the user):

1. Urls.xlsx columns: State | Code | Website | Status | Last Comp Date | File Path.
   Each row's Website is opened. Completed rows are skipped on rerun.

2. Per row: Website -> BEGIN SEARCH -> ACCEPT -> filters -> dates -> Search
   -> 100 rows/page -> scrape all data-rk -> download each filing
   (HTML + ZIP saved under Output Data/{YYYY-MM-DD}/{Code}/{trackingId}.{zip,html}).

3. After EVERY driver.get / refresh: check for
   (a) blocked/captcha  -> solve,
   (b) session-expired  -> raise SessionExpiredRetry (typed signal).

4. Session expiry:
   - During a filing page (Phase 2): refresh session on the SAME browser by
     hitting the current row's Website -> ACCEPT (no filters, no search),
     then jump back to the filing URL and retry that row.
   - During page scraping (Phase 1): refresh session, re-run filters + search,
     then resume scraping from pages_collected + 1.

5. Filing with no Download ZIP button: appended to not_down.json (status="skipped"),
   never retried. At end of run, Not down.xlsx is derived from not_down.json.

6. A state row in Urls.xlsx becomes "Completed" ONLY when every filing for
   that state downloaded BOTH HTML and ZIP (i.e., completed_data_rk contains
   every collected id, skipped_data_rk is empty for the state). Otherwise
   the row stays "Partial" and is retried on the next run.

Progress state (serff_progress.json / serff_table_data.json) is keyed by
criteria INCLUDING the row's Code, so each state gets its own store — no
cross-state contamination of IDs or completed/skipped status.

Run:
    python3 -m src.automation.serff
"""
import json
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, date
from pathlib import Path
from random import randint

import pandas as pd
from selenium.common.exceptions import InvalidSessionIdException, NoSuchWindowException
from selenium.common.exceptions import (
    StaleElementReferenceException,
    TimeoutException,
    UnexpectedAlertPresentException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

from src.commons import *
from src.automation.driver import Browser            # <-- ADJUST if path differs
from src.logging import logger


# =============================================================
# EXCEPTIONS
# =============================================================

class SessionAbort(Exception):
    """Too many consecutive failures; stop the run (JSON state is kept for resume)."""


class DateWindowError(Exception):
    """serff_window.json is missing, unreadable, or malformed."""


class ZipNotAvailable(Exception):
    """The filing page has no Download ZIP button / ZIP file."""


class SessionExpiredRetry(Exception):
    """Session-expired interstitial encountered during navigation / wait."""


# =============================================================
# DATE-WINDOW FILE  (REQUIRED)
# =============================================================

class DateWindow:
    def __init__(self, path):
        self.path = Path(path)
        self.start, self.end = self._load_required()

    def _load_required(self):
        if not self.path.is_file():
            raise DateWindowError(
                f"Missing date window file: {self.path}\n"
                f'Create it with: {{"start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD"}}'
            )
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            start = date.fromisoformat(str(data["start_date"]))
            end = date.fromisoformat(str(data["end_date"]))
        except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
            raise DateWindowError(f"Invalid date window file {self.path}: {exc}") from exc
        if start > end:
            raise DateWindowError(f"start_date ({start}) is after end_date ({end}) in {self.path}")
        logger.info(f"Date window loaded: {start.isoformat()} .. {end.isoformat()}")
        return start, end

    def _save(self, start: date, end: date):
        payload = {"start_date": start.isoformat(), "end_date": end.isoformat()}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        logger.info(f"Date window saved: {self.path} -> {payload}")

    def set(self, start: date, end: date):
        if start > end:
            raise DateWindowError(f"Refusing to save inverted window: {start} > {end}")
        self.start, self.end = start, end
        self._save(start, end)

    def roll_back_to_previous_month(self):
        new_start = self._shift_month(self.start, -1)
        new_end = self._shift_month(self.end, -1)
        self.set(new_start, new_end)
        return new_start, new_end

    @staticmethod
    def _shift_month(d: date, delta_months: int) -> date:
        month = d.month + delta_months
        year = d.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        next_month = date(year + (month == 12), (month % 12) + 1, 1)
        last_day = (next_month - pd.Timedelta(days=1)).day
        return date(year, month, min(d.day, last_day))

    @property
    def start_mmddyy(self) -> str:
        return self.start.strftime("%m/%d/%y")

    @property
    def end_mmddyy(self) -> str:
        return self.end.strftime("%m/%d/%y")

    def as_dict(self) -> dict:
        return {"start_date": self.start.isoformat(), "end_date": self.end.isoformat()}


# =============================================================
# CONFIGURATION
# =============================================================

@dataclass
class SerffConfig:
    state: str = "AL"

    element_timeout: int = 30
    page_load_timeout: int = 60
    search_result_timeout: int = 60
    verification_timeout: int = 300

    max_captcha_attempts: int = 3
    post_solve_wait: int = 3

    download_wait: int = 20
    rows_per_page: str = "100"

    max_refreshes: int = 3
    refresh_backoff: int = 3
    max_row_retries: int = 3
    max_collect_failures: int = 6
    max_consecutive_restarts: int = 5
    max_session_attempts: int = 5
    restart_cooldown: int = 10

    business_type_index: int = 2
    insurance_prefix_allowlist: tuple = ("A", "CR", "H", "L", "LTC", "MS")
    insurance_prefix_blocklist: tuple = ("HOrg",)
    expected_tois_count: int | None = None

    site_home_url: str = "https://serff-sfa.naic.org/serff/sfa/"
    filing_url_template: str = (
        "https://serff-sfa.naic.org/serff/sfa/search/filingSummary.xhtml?filingId={id}"
    )

    base_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent)
    download_dir: Path = field(
        default_factory=lambda: Path(__file__).resolve().parent / "Downloads"
    )
    output_root: Path = field(
        default_factory=lambda: Path(__file__).resolve().parent / "Output Data"
    )

    not_downloaded_json: str = "not_down.json"
    not_downloaded_xlsx: str = "Not down.xlsx"
    input_csv = "Urls.xlsx"
    window_json = "date.json"

    @property
    def progress_json(self): return self.base_dir / "serff_progress.json"
    @property
    def table_data_json(self): return self.base_dir / "serff_table_data.json"
    @property
    def window_json_path(self): return self.base_dir / self.window_json
    @property
    def not_downloaded_json_path(self): return self.base_dir / self.not_downloaded_json
    @property
    def not_downloaded_xlsx_path(self): return self.base_dir / self.not_downloaded_xlsx
    @property
    def today_dir(self): return self.output_root / datetime.now().strftime("%Y-%m-%d")

    def filing_url(self, data_rk):
        return self.filing_url_template.format(id=data_rk)

    def _safe(self, value, fallback):
        return re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or fallback))

    def code_dir(self, code) -> Path:
        return self.today_dir / self._safe(code, "unknown")

    def filing_dir(self, code) -> Path:
        return self.code_dir(code)

    def filing_zip_path(self, code, tracking_num) -> Path:
        name = self._safe(tracking_num, "unknown")
        return self.filing_dir(code) / f"{name}.zip"

    def filing_html_path(self, code, tracking_num) -> Path:
        name = self._safe(tracking_num, "unknown")
        return self.filing_dir(code) / f"{name}.html"

    def criteria(self, start_date: str, end_date: str, input_code: str = ""):
        return {
            "state": self.state,
            "input_code": str(input_code or "").strip(),
            "start_date": start_date,
            "end_date": end_date,
            "business_type_index": self.business_type_index,
            "insurance_prefix_allowlist": list(self.insurance_prefix_allowlist),
            "insurance_prefix_blocklist": list(self.insurance_prefix_blocklist),
            "rows_per_page": self.rows_per_page,
        }


# =============================================================
# LOCATORS
# =============================================================

class Locators:
    BEGIN_SEARCH = (By.XPATH, '//a[contains(concat(" ", normalize-space(@class), " "), " btn-success ")]')
    ACCEPT = (By.XPATH, '//button[@id="j_idt18:j_idt20"]')
    START_DATE = (By.XPATH, '//input[@id="simpleSearch:submissionStartDate_input"]')
    END_DATE = (By.XPATH, '//input[@id="simpleSearch:submissionEndDate_input"]')
    SEARCH = (By.XPATH, '//button[@id="simpleSearch:saveBtn"]')
    BUSINESS_TYPE = (By.XPATH, '//select[@id="simpleSearch:businessType_input"]')
    SERFF_TRACKING_NUM = (
        By.XPATH,
        "//*[contains(text(),'SERFF Tracking Number: ')]/following-sibling::*",
    )
    INSURANCE_TRIGGER = (
        By.XPATH,
        '//*[contains(@class,"ui-selectcheckboxmenu")]'
        '[.//input[contains(@id,"availableTois") or contains(@name,"availableTois")]]'
        '//*[contains(@class,"ui-selectcheckboxmenu-trigger")]',
    )
    INSURANCE_ITEMS = (
        By.XPATH,
        '//*[contains(@class,"ui-selectcheckboxmenu-items-wrapper")]//ul/li',
    )
    RESULT_ROWS = (
        By.XPATH,
        '//tbody[contains(concat(" ", normalize-space(@class), " "), " ui-datatable-data ")]/tr',
    )
    ROWS_PER_PAGE = (By.XPATH, '//select[@id="j_idt25:filingTable:j_id2"]')
    NEXT_PAGE = (
        By.XPATH,
        '//*[contains(@class,"ui-paginator-next") or contains(@class,"ui-icon-seek-next")]',
    )
    PAGINATOR_LABEL = (
        By.XPATH,
        '//*[contains(@class,"ui-paginator-current") or contains(@class,"ui-paginator-pages")]',
    )
    TOTAL_FILINGS = (By.XPATH, '//span[contains(@class,"col-sm-6")]/span[1]')
    DOWNLOAD_ZIP = (By.XPATH, '//button[@id="summaryForm:downloadLink"]')

    @staticmethod
    def page_link(number):
        return (By.XPATH,
                f'//a[contains(@class,"ui-paginator-page") and @aria-label="Page {number}"]')


# =============================================================
# PROGRESS / TABLE DATA STORE
# =============================================================

class ProgressStore:
    """
    completed_data_rk : successfully downloaded (ZIP + HTML saved)
    skipped_data_rk   : terminal (no ZIP, or max retries exhausted); NEVER retried
    """

    def __init__(self, progress_path, table_path, criteria):
        self.progress_path = Path(progress_path)
        self.table_path = Path(table_path)
        self.criteria = criteria
        self.progress = self._load_progress()
        self.table = self._load_table()

    @staticmethod
    def _save_json_atomic(path, payload):
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        tmp.replace(path)
        logger.info(f"JSON saved: {path}")

    @staticmethod
    def _load_json(path, default):
        if not path.is_file():
            return default
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            logger.exception(f"Could not read {path}; starting fresh.")
            return default

    @staticmethod
    def _now():
        return datetime.now().isoformat(timespec="seconds")

    @staticmethod
    def _new_progress():
        return {
            "completed_data_rk": [],
            "completed_count": 0,
            "unconfirmed_data_rk": [],
            "skipped_data_rk": {},
            "failed": {},
            "updated_at": None,
        }

    def _new_table(self):
        return {
            "meta": {
                "criteria": self.criteria,
                "collection_complete": False,
                "pages_collected": 0,
                "total_pages": None,
                "expected_total": None,
                "updated_at": None,
            },
            "records": {},
        }

    def _load_progress(self):
        data = self._load_json(self.progress_path, self._new_progress())
        for key, value in self._new_progress().items():
            data.setdefault(key, value)
        data["completed_count"] = len(data["completed_data_rk"])
        return data

    def _load_table(self):
        data = self._load_json(self.table_path, None)
        if not data or "meta" not in data:
            return self._new_table()

        stored = data["meta"].get("criteria") or {}
        current = self.criteria

        # Migration: if the stored criteria is a legacy format (no input_code),
        # do NOT wipe — adopt the new criteria on the existing store, as long as
        # the meaningful fields (state / date window) match.
        if "input_code" not in stored and "input_code" in current:
            if (stored.get("state") == current["state"]
                    and stored.get("start_date") == current["start_date"]
                    and stored.get("end_date") == current["end_date"]):
                logger.info(
                    "Legacy store detected (no input_code); migrating in place."
                )
                data["meta"]["criteria"] = current
                self._save_json_atomic(self.table_path, data)
                self.progress = self._load_progress()
                data.setdefault("records", {})
                return data
            # If state/window differ, fall through to normal archive logic.

        if stored != current:
            # Only wipe when the state / date window really changed.
            # A pure input_code change is not enough reason to lose progress.
            significant_keys = ("state", "start_date", "end_date")
            same_meaningful = all(
                stored.get(k) == current.get(k) for k in significant_keys
            )
            if same_meaningful:
                logger.info(
                    "Criteria differ only in non-significant keys "
                    "(e.g. input_code); keeping existing store."
                )
                data["meta"]["criteria"] = current
                self._save_json_atomic(self.table_path, data)
            else:
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                logger.warning("Saved ids belong to a different search; archiving old JSON files.")
                for path in (self.table_path, self.progress_path):
                    if path.is_file():
                        path.rename(path.with_name(f"{path.name}.stale-{stamp}"))
                self.progress = self._new_progress()
                return self._new_table()

        data.setdefault("records", {})
        return data

    @property
    def meta(self): return self.table["meta"]
    @property
    def records(self): return self.table["records"]
    @property
    def collection_complete(self): return bool(self.meta.get("collection_complete"))
    @property
    def pages_collected(self): return int(self.meta.get("pages_collected") or 0)

    def add_page(self, page_no, total_pages, page_records):
        for record in page_records:
            if record.get("data_rk"):
                self.records[record["data_rk"]] = record
        self.meta["pages_collected"] = page_no
        self.meta["total_pages"] = total_pages
        self.meta["updated_at"] = self._now()
        self._save_json_atomic(self.table_path, self.table)

    def mark_collection_complete(self, expected_total):
        self.meta["collection_complete"] = True
        self.meta["expected_total"] = expected_total
        self.meta["updated_at"] = self._now()
        self._save_json_atomic(self.table_path, self.table)

    @property
    def completed_keys(self): return set(self.progress["completed_data_rk"])
    @property
    def completed_count(self): return len(self.progress["completed_data_rk"])
    @property
    def skipped_keys(self): return set((self.progress.get("skipped_data_rk") or {}).keys())

    def pending_keys(self):
        done = self.completed_keys
        skipped = self.skipped_keys
        return [rk for rk in self.records if rk not in done and rk not in skipped]

    def mark_completed(self, data_rk, confirmed=True):
        if data_rk not in self.completed_keys:
            self.progress["completed_data_rk"].append(data_rk)
        unconfirmed = self.progress["unconfirmed_data_rk"]
        if not confirmed and data_rk not in unconfirmed:
            unconfirmed.append(data_rk)
        self.progress["failed"].pop(data_rk, None)
        self.progress.setdefault("skipped_data_rk", {}).pop(data_rk, None)
        self.progress["completed_count"] = self.completed_count
        self.progress["last_completed_data_rk"] = data_rk
        self.progress["updated_at"] = self._now()
        self._save_json_atomic(self.progress_path, self.progress)

    def mark_skipped(self, data_rk, reason):
        bucket = self.progress.setdefault("skipped_data_rk", {})
        bucket[data_rk] = {"reason": str(reason)[:300], "at": self._now()}
        self.progress["failed"].pop(data_rk, None)
        self.progress["updated_at"] = self._now()
        self._save_json_atomic(self.progress_path, self.progress)
        logger.info(f"Marked data-rk={data_rk} as SKIPPED (terminal): {reason}")

    def is_skipped(self, data_rk):
        return data_rk in (self.progress.get("skipped_data_rk") or {})


# =============================================================
# SERFF AUTOMATION
# =============================================================

class Serff:
    PROBLEM_MARKERS = {
        "bad_gateway": ("502 bad gateway", "bad gateway", "503 service unavailable",
                        "service unavailable", "504 gateway", "gateway time-out",
                        "gateway timeout"),
        "session_expired": ("session expired", "session has expired", "session timed out",
                            "view expired", "viewexpired"),
        "blocked": ("access is temporarily restricted",),
    }
    HARD_PROBLEMS = {"session_expired", "blocked", "driver_dead"}

    _TICK_BY_PREFIX_JS = r"""
        var allow = arguments[0];
        var block = arguments[1];
        function matches(v, prefixes) {
            v = (v || "").trim().toUpperCase();
            for (var i = 0; i < prefixes.length; i++) {
                var p = prefixes[i].toUpperCase();
                if (v.indexOf(p) === 0 && /[0-9]/.test(v.charAt(p.length))) {
                    return true;
                }
            }
            return false;
        }
        function classify(v) {
            if (matches(v, block)) return false;
            return matches(v, allow);
        }
        var inputs = Array.prototype.slice.call(
            document.querySelectorAll('input[name="simpleSearch:availableTois"]'));
        var lis = Array.prototype.slice.call(
            document.querySelectorAll('.ui-selectcheckboxmenu-items-wrapper ul li'));
        var liByValue = {};
        for (var i = 0; i < lis.length; i++) {
            var lbl = lis[i].querySelector('label');
            if (lbl) liByValue[(lbl.textContent || "").trim()] = lis[i];
        }
        var ticked = [], skipped = [];
        for (var j = 0; j < inputs.length; j++) {
            var val = (inputs[j].value || "").trim();
            var should = classify(val);
            (should ? ticked : skipped).push(val);
            inputs[j].checked = should;
            inputs[j].setAttribute('aria-checked', should ? 'true' : 'false');
            var li = liByValue[val];
            if (li) {
                var box  = li.querySelector('.ui-chkbox-box');
                var icon = li.querySelector('.ui-chkbox-icon');
                var aria = li.querySelector('input[type="checkbox"]');
                li.classList.toggle('ui-selectcheckboxmenu-checked',   should);
                li.classList.toggle('ui-selectcheckboxmenu-unchecked', !should);
                if (box)  box.classList.toggle('ui-state-active', should);
                if (icon) {
                    icon.classList.toggle('ui-icon-check', should);
                    icon.classList.toggle('ui-icon-blank', !should);
                }
                if (aria) aria.setAttribute('aria-checked', should ? 'true' : 'false');
            }
        }
        var w = null;
        try { w = PrimeFaces.getWidgetById('simpleSearch:availableTois'); } catch (e) {}
        if (w) {
            try { if (w.updateLabel)   { w.updateLabel();   } } catch (e) {}
            try { if (w.updateToggler) { w.updateToggler(); } } catch (e) {}
            try { if (w.hasBehavior && w.hasBehavior('change')) {
                    w.callBehavior('change');
                } } catch (e) {}
        }
        var label = null;
        var span = document.getElementById('simpleSearch:toisSelected');
        if (span) {
            var m = (span.textContent || "").match(/(\d+)/);
            if (m) label = parseInt(m[1], 10);
        }
        var checked = document.querySelectorAll(
            'input[name="simpleSearch:availableTois"]:checked').length;
        return {
            totalInputs: inputs.length, totalLis: lis.length,
            wantChecked: ticked.length, pageLabel: label, checkedNow: checked,
            ticked: ticked, skipped: skipped
        };
    """

    def __init__(self, config=None, browser=None):
        self.config = config or SerffConfig()
        self.window = DateWindow(self.config.window_json_path)
        self.browser = browser or Browser(
            headless=False, download_dir=str(self.config.download_dir),
        )
        self.driver = None
        self._restarts = 0
        self._collect_failures = 0
        self._last_filing_url = None
        self._current_site_url = None
        self._make_store()

    def _make_store(self, input_code: str = ""):
        self.store = ProgressStore(
            self.config.progress_json,
            self.config.table_data_json,
            self.config.criteria(
                self.window.start_mmddyy, self.window.end_mmddyy,
                input_code=input_code,
            ),
        )

    # ---------------------------------------------------------
    # Driver
    # ---------------------------------------------------------
    def start_browser(self):
        logger.info("Starting Chrome WebDriver.")
        self.driver = self.browser.get_chrome()
        self.driver.set_page_load_timeout(self.config.page_load_timeout)
        self.config.download_dir.mkdir(parents=True, exist_ok=True)
        self.config.today_dir.mkdir(parents=True, exist_ok=True)

    def close(self):
        if not self.driver:
            return
        try:
            self.driver.quit()
        except Exception:
            logger.exception("Error while closing browser.")
        finally:
            self.driver = None

    # ---------------------------------------------------------
    # Small wrappers
    # ---------------------------------------------------------
    def _screenshot(self, name):
        if self.driver is None:
            return None
        try:
            return save_screenshot(self.driver, name)
        except WebDriverException as exc:
            logger.warning(f"Screenshot skipped ({type(exc).__name__}): {name}")
            return None

    def _log_page_info(self):
        try:
            logger.info(f"Current URL: {self.driver.current_url}")
            logger.info(f"Page title: {self.driver.title}")
        except WebDriverException:
            logger.exception("Unable to read page information.")

    def _wait_visible(self, locator, description, timeout=None):
        timeout = timeout or self.config.element_timeout
        try:
            return wait_for_element(self.driver, locator, timeout=timeout,
                                    condition="visible", raise_on_timeout=True)
        except TimeoutException:
            self._screenshot("element_timeout")
            raise

    def _click(self, locator, description, timeout=None):
        timeout = timeout or self.config.element_timeout
        element = WebDriverWait(self.driver, timeout).until(
            EC.presence_of_element_located(locator)
        )
        self.driver.execute_script(
            "arguments[0].scrollIntoView({block:'center', behavior:'instant'});", element
        )
        time.sleep(0.5)
        try:
            WebDriverWait(self.driver, timeout).until(
                EC.element_to_be_clickable(locator)
            ).click()
        except WebDriverException:
            element = WebDriverWait(self.driver, timeout).until(
                EC.presence_of_element_located(locator)
            )
            self.driver.execute_script("arguments[0].click();", element)
        return element

    def _scroll_to_bottom(self):
        self.driver.execute_script(
            "window.scrollTo(0, Math.max(document.body.scrollHeight,"
            " document.documentElement.scrollHeight));"
        )

    def _wait_ajax_idle(self, timeout=15):
        script = (
            "var pf = true, jq = true;"
            "try { if (window.PrimeFaces && PrimeFaces.ajax && PrimeFaces.ajax.Queue)"
            " pf = PrimeFaces.ajax.Queue.isEmpty(); } catch (e) {}"
            "try { if (window.jQuery) jq = (jQuery.active === 0); } catch (e) {}"
            "return pf && jq;"
        )
        try:
            WebDriverWait(self.driver, timeout, poll_frequency=0.1).until(
                lambda d: d.execute_script(script)
            )
        except TimeoutException:
            logger.warning("Ajax did not go idle in time; continuing.")

    # ---------------------------------------------------------
    # Rule 3: problem detection after every driver.get
    # ---------------------------------------------------------
    def _is_session_expired_page(self):
        try:
            url = (self.driver.current_url or "").lower()
            if "expired" in url:
                return True
            html = (self.driver.page_source or "").lower()
            return any(m in html for m in (
                "session expired", "session has expired",
                "session timed out", "view expired", "viewexpired",
            ))
        except WebDriverException:
            return False
    def _is_server_error_page(self):
        """True if the page is a SERFF HTTP 5xx error page."""
        try:
            html = (self.driver.page_source or "").lower()
            if "http 500" in html or "http 502" in html or "http 503" in html or "http 504" in html:
                return True
            if "internal server error" in html or "bad gateway" in html or "service unavailable" in html:
                return True
            return False
        except WebDriverException:
            return False
            
    def _page_problem(self):
        if self.driver is None:
            return "driver_dead"
        try:
            title = (self.driver.title or "").lower()
            url = (self.driver.current_url or "").lower()
            text = (self.driver.execute_script(
                "return document.body ? document.body.innerText.substring(0, 3000) : '';"
            ) or "").lower()
        except UnexpectedAlertPresentException:
            self._accept_alert()
            return "alert"
        except WebDriverException:
            # This catches InvalidSessionIdException, NoSuchWindowException, etc.
            return "driver_dead"

        haystack = f"{title}\n{text}"
        for problem, markers in self.PROBLEM_MARKERS.items():
            if any(m in haystack for m in markers):
                return problem
        if "expired" in url:
            return "session_expired"
        if self._is_verification_page():
            return "captcha"
        if not text.strip():
            return "blank"
        return None
    
    def _refresh(self):
        try:
            self.driver.refresh()
        except TimeoutException:
            logger.warning("Refresh timed out; checking current page.")

    def _load_page(self, url):
        """Navigate to `url`; verify healthy. Order: CAPTCHA -> EXPIRY -> 5xx -> DEAD."""
        problem = None
        for attempt in range(self.config.max_refreshes + 1):
            if attempt == 0:
                try:
                    self.driver.get(url)
                except TimeoutException:
                    logger.warning("Navigation timed out. Checking current page.")
                except WebDriverException as exc:
                    # If get() fails immediately, the driver might be dead
                    logger.error(f"Driver failed to navigate: {exc}")
                    raise RuntimeError("driver_dead") from exc
            else:
                logger.warning(f"Page problem '{problem}'; refresh {attempt}/{self.config.max_refreshes}.")
                time.sleep(self.config.refresh_backoff * attempt)
                self._refresh()

            # ---- 1) CAPTCHA FIRST -----------------------------------------
            if self._is_verification_page():
                logger.warning(f"CAPTCHA detected on {url}; attempting to solve.")
                if not self.handle_verification():
                    raise RuntimeError("Captcha could not be solved.")
                if self._is_verification_page():
                    raise RuntimeError("Captcha still present after solving.")
                if self._is_session_expired_page():
                    raise SessionExpiredRetry(f"session expired after captcha on {url}")
                if self._is_server_error_page():
                    raise SessionExpiredRetry(f"HTTP 5xx on {url} (after captcha)")
                
                problem = self._page_problem()
                if problem is None or problem not in self.HARD_PROBLEMS:
                    return

            # ---- 2) SESSION-EXPIRED ---------------------------------------
            if self._is_session_expired_page():
                raise SessionExpiredRetry(f"session expired after navigating to {url}")

            # ---- 3) HTTP 5xx ----------------------------------------------
            if self._is_server_error_page():
                logger.warning(f"HTTP 5xx detected on {url}; will refresh session and retry.")
                raise SessionExpiredRetry(f"HTTP 5xx on {url}")

            # ---- post-settle re-checks ------------------------------------
            time.sleep(1)
            
            if self._is_verification_page():
                logger.warning(f"CAPTCHA appeared post-settle on {url}.")
                if not self.handle_verification():
                    raise RuntimeError("Captcha could not be solved.")
            if self._is_session_expired_page():
                raise SessionExpiredRetry(f"session expired on {url} (post-settle)")
            if self._is_server_error_page():
                raise SessionExpiredRetry(f"HTTP 5xx on {url} (post-settle)")

            problem = self._page_problem()
            if problem is None:
                return
            if problem == "captcha":
                if self.handle_verification():
                    return
                raise RuntimeError("Captcha could not be solved.")
            if problem == "session_expired":
                raise SessionExpiredRetry(f"session expired on {url}")
            if problem in self.HARD_PROBLEMS:
                raise RuntimeError(f"Page problem requiring a new session: {problem}")

        raise RuntimeError(f"Page still broken after {self.config.max_refreshes} refreshes: {problem}")
    
    def _wait_with_refresh(self, locator, description, condition="visible"):
        tries = self.config.max_refreshes
        for attempt in range(tries + 1):
            try:
                return wait_for_element(
                    self.driver, locator, timeout=self.config.element_timeout,
                    condition=condition, raise_on_timeout=True,
                )
            except TimeoutException:
                problem = self._page_problem()
                self._screenshot("element_timeout")
                if attempt == tries:
                    raise
                if problem == "captcha":
                    if not self.handle_verification():
                        raise RuntimeError("Captcha could not be solved.")
                elif problem == "session_expired":
                    raise SessionExpiredRetry(f"session expired while waiting for {description}")
                elif problem in self.HARD_PROBLEMS:
                    raise RuntimeError(f"Page problem requiring a new session: {problem}")
                else:
                    time.sleep(self.config.refresh_backoff * (attempt + 1))
                    self._refresh()

    # ---------------------------------------------------------
    # Verification
    # ---------------------------------------------------------
    def _is_verification_page(self):
        try:
            return bool(is_verification_page(self.driver))
        except (InvalidSessionIdException, NoSuchWindowException):
            raise
        except Exception:
            return False

    def handle_verification(self):
        if not self._is_verification_page():
            return True
        self._screenshot("human_verification")
        deadline = time.monotonic() + self.config.verification_timeout
        attempts = 0
        while time.monotonic() < deadline and attempts < self.config.max_captcha_attempts:
            attempts += 1
            try:
                solve_capctha(self.driver)
            except Exception:
                logger.exception("CAPTCHA solver failed.")
                return False
            time.sleep(self.config.post_solve_wait)
            if not self._is_verification_page():
                return True
        logger.error("Human verification was not completed.")
        return False

    def _accept_alert(self):
        try:
            alert = WebDriverWait(self.driver, 5).until(EC.alert_is_present())
            alert.accept()
            return True
        except Exception:
            return False

    def _dismiss_blocking_dialogs(self, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                alert = WebDriverWait(self.driver, 1).until(EC.alert_is_present())
                alert.accept()
            except TimeoutException:
                break
            except (UnexpectedAlertPresentException, WebDriverException):
                break

    def _close_extra_windows(self, original_handles):
        try:
            current_handles = set(self.driver.window_handles)
        except WebDriverException:
            return
        extra = current_handles - original_handles
        for handle in extra:
            try:
                self.driver.switch_to.window(handle)
                self.driver.close()
            except WebDriverException:
                logger.exception(f"Could not close window {handle}.")
        main = original_handles & current_handles or (current_handles - extra)
        if main:
            try:
                self.driver.switch_to.window(next(iter(main)))
            except WebDriverException:
                logger.exception("Could not switch back to the main window.")

    # ---------------------------------------------------------
    # Session flow
    # ---------------------------------------------------------
    def open_site(self, url=None):
        if url:
            self._load_page(url)
        return self.handle_verification()

    def begin_search(self, attempt=None):
        attempts = attempt if attempt is not None else self.config.max_session_attempts
        for try_no in range(1, attempts + 1):
            try:
                logger.info(f"STEP 3: Clicking BEGIN SEARCH (attempt {try_no}/{attempts}).")
                element = self._wait_with_refresh(Locators.BEGIN_SEARCH, "BEGIN SEARCH link")
                old_url = self.driver.current_url
                element.click()
                try:
                    WebDriverWait(self.driver, self.config.element_timeout).until(
                        lambda d: d.current_url != old_url
                    )
                except TimeoutException:
                    logger.warning("URL did not change after BEGIN SEARCH.")
                time.sleep(random.uniform(1, 3))
                self._log_page_info()
                return self.handle_verification()
            except SessionExpiredRetry as exc:
                logger.warning(f"BEGIN SEARCH: {exc}; re-navigating and retrying.")
                if try_no == attempts:
                    raise
                self._load_page(self._current_site_url or self.config.site_home_url)
        return False

    def accept_terms(self, attempt=5):
        if attempt == 0:
            self._screenshot("accept_terms_stuck")
            return False
        logger.info(f"STEP 4: Clicking ACCEPT (attempt {6 - attempt}/5).")
        try:
            self._wait_with_refresh(Locators.ACCEPT, "ACCEPT button", condition="presence")
            self._click(Locators.ACCEPT, "ACCEPT button")
        except SessionExpiredRetry as exc:
            logger.warning(f"ACCEPT: {exc}; re-navigating and retrying from BEGIN SEARCH.")
            self._load_page(self._current_site_url or self.config.site_home_url)
            if not self.begin_search():
                return False
            return self.accept_terms(attempt=attempt - 1)

        if not self.handle_verification():
            return False
        try:
            WebDriverWait(self.driver, 5).until(
                EC.presence_of_element_located(
                    (By.XPATH, "//*[contains(normalize-space(.), 'Filing Search')]")
                )
            )
        except TimeoutException:
            try:
                self._refresh()
            except WebDriverException:
                return False
            return self.accept_terms(attempt=attempt - 1)

        if self._is_session_expired_page():
            logger.warning("ACCEPT: session expired right after form load; retrying.")
            self._load_page(self._current_site_url or self.config.site_home_url)
            if not self.begin_search():
                return False
            return self.accept_terms(attempt=attempt - 1)

        self._wait_with_refresh(Locators.START_DATE, "Start Submission Date input")
        return True

    def _accept_terms_only(self, attempt=5):
        if attempt == 0:
            self._screenshot("accept_terms_only_stuck")
            return False
        logger.info(f"STEP 4: Clicking ACCEPT (attempt {6 - attempt}/5, no filter wait).")
        try:
            self._wait_with_refresh(Locators.ACCEPT, "ACCEPT button", condition="presence")
            self._click(Locators.ACCEPT, "ACCEPT button")
        except SessionExpiredRetry as exc:
            logger.warning(f"ACCEPT: {exc}; re-navigating and retrying.")
            self._load_page(self._current_site_url or self.config.site_home_url)
            if not self.begin_search():
                return False
            return self._accept_terms_only(attempt=attempt - 1)

        if not self.handle_verification():
            return False
        try:
            WebDriverWait(self.driver, 5).until(
                EC.presence_of_element_located(
                    (By.XPATH, "//*[contains(normalize-space(.), 'Filing Search')]")
                )
            )
        except TimeoutException:
            try:
                self._refresh()
            except WebDriverException:
                return False
            return self._accept_terms_only(attempt=attempt - 1)

        if self._is_session_expired_page():
            logger.warning("ACCEPT: session expired right after form load; retrying.")
            self._load_page(self._current_site_url or self.config.site_home_url)
            if not self.begin_search():
                return False
            return self._accept_terms_only(attempt=attempt - 1)
        return True

    def establish_session(self, url=None):
        if self.driver is None:
            self.start_browser()
        return self.open_site(url) and self.begin_search() and self.accept_terms()

    def establish_session_with_retries(self, url=None):
        for attempt in range(1, self.config.max_session_attempts + 1):
            try:
                if self.establish_session(url):
                    return
                logger.error("Session set-up did not finish (verification?).")
            except SessionExpiredRetry as exc:
                logger.warning(f"SessionExpiredRetry during session set-up: {exc}")
                try:
                    if self.driver is not None:
                        self._load_page(url or self._current_site_url or self.config.site_home_url)
                        continue
                except Exception:
                    logger.exception("Retry after session expiry failed.")
            except SessionAbort:
                raise
            except Exception:
                logger.exception(f"Session set-up failed (attempt {attempt}).")
                self._screenshot("session_setup_failure")
                
                # --- REUSE THE SAME BROWSER ---
                # Do NOT call self.close(). Just try to reset the page.
                if self.driver is not None:
                    try:
                        target_url = url or self._current_site_url or self.config.site_home_url
                        logger.info(f"Restarting session flow by re-hitting: {target_url}")
                        self._load_page(target_url)
                    except Exception:
                        logger.exception("Failed to reload page; will retry anyway.")
                else:
                    # Only start a new browser if it was already dead
                    self.start_browser()
            
            time.sleep(self.config.restart_cooldown * attempt)
            
        raise SessionAbort("Could not establish a SERFF session.")
    # ---------------------------------------------------------
    # Rule 4: SAME-browser session refresh
    # ---------------------------------------------------------
    def _refresh_session_same_browser(self):
        """Refresh the SERFF session without quitting Chrome.
        Hits _current_site_url -> BEGIN SEARCH -> ACCEPT (no filters)."""
        if self.driver is None:
            self.start_browser()
        entry_url = self._current_site_url or self.config.site_home_url
        logger.info(f"REFRESH: opening {entry_url}")
        self._load_page(entry_url)
        if not self.begin_search():
            raise SessionAbort("Could not refresh session (BEGIN SEARCH failed).")
        if not self._accept_terms_only():
            raise SessionAbort("Could not refresh session (ACCEPT failed).")

    def restart_session_and_resume(self, target_url=None):
        """Phase-2 session refresh: same browser, refresh session, then jump
        straight back to the filing URL we were on. NO filters, NO search."""
        self._restarts += 1
        if self._restarts > self.config.max_consecutive_restarts:
            raise SessionAbort(
                f"{self.config.max_consecutive_restarts} session restarts in a row without progress."
            )
        self._refresh_session_same_browser()
        if target_url:
            logger.info(f"RESUME: Navigating back to {target_url} (no filters, no search).")
            self._load_page(target_url)

    def restart_session(self):
        """Phase-1 session refresh: same browser, refresh session.  Filters
        and search will be re-applied by `prepare_search` in collect_ids."""
        self._restarts += 1
        if self._restarts > self.config.max_consecutive_restarts:
            raise SessionAbort(
                f"{self.config.max_consecutive_restarts} session restarts in a row without progress."
            )
        self._refresh_session_same_browser()

    # ---------------------------------------------------------
    # Filters
    # ---------------------------------------------------------
    def _select_business_type(self):
        index = self.config.business_type_index
        select_el = WebDriverWait(self.driver, self.config.element_timeout).until(
            EC.presence_of_element_located(Locators.BUSINESS_TYPE)
        )
        options = select_el.find_elements(By.TAG_NAME, "option")
        if index >= len(options):
            raise RuntimeError(f"Business Type index {index} out of range.")
        expected_label = options[index].text.strip()
        if "Life" not in expected_label:
            raise RuntimeError(
                f"Business Type[{index}] is '{expected_label}', expected 'Life'."
            )
        Select(select_el).select_by_index(index)
        self.driver.execute_script(
            "arguments[0].dispatchEvent(new Event('input',  {bubbles:true}));"
            "arguments[0].dispatchEvent(new Event('change', {bubbles:true}));",
            select_el,
        )
        self._wait_ajax_idle()
        time.sleep(randint(1, 3))

    def _read_tracking_number(self, timeout=None):
        timeout = timeout or self.config.element_timeout
        try:
            el = WebDriverWait(self.driver, timeout).until(
                EC.presence_of_element_located(Locators.SERFF_TRACKING_NUM)
            )
            WebDriverWait(self.driver, timeout).until(
                lambda d: (el.text or "").strip() != ""
            )
            return el.text.strip()
        except TimeoutException:
            self._screenshot("tracking_number_missing")
            raise RuntimeError("Could not read SERFF Tracking Number from filing page.")

    def _select_insurance_types(self):
        allow = list(self.config.insurance_prefix_allowlist)
        block = list(self.config.insurance_prefix_blocklist)
        def _state():
            return self.driver.execute_script(self._TICK_BY_PREFIX_JS, allow, block)
        result = _state()
        self._wait_ajax_idle()
        time.sleep(1)
        want, have = result["wantChecked"], result["checkedNow"]
        if self.config.expected_tois_count is not None and want != self.config.expected_tois_count:
            self._screenshot("tois_count_mismatch")
            raise RuntimeError(
                f"Type of Insurance count drifted: classify() produced {want}, "
                f"config expects {self.config.expected_tois_count}."
            )
        if have != want:
            time.sleep(1)
            result = _state()
            want, have = result["wantChecked"], result["checkedNow"]
            if have != want:
                self._screenshot("insurance_selection_mismatch")
                raise RuntimeError(
                    f"Type of Insurance DOM mismatch: expected {want}, got {have}."
                )
        logger.info(f"FILTER: {want} of {result['totalInputs']} Type-of-Insurance codes ticked.")

    def apply_search_filters(self):
        self._select_business_type()
        self._select_insurance_types()

    def _enter_date(self, locator, value, label):
        field_el = self._wait_visible(locator, f"{label} input")
        field_el.clear()
        field_el.send_keys(value)
        if field_el.get_attribute("value") != value:
            self._screenshot(f"{label.replace(' ', '_').lower()}_validation")
            return False
        return True

    def enter_dates(self):
        return (
            self._enter_date(Locators.START_DATE, self.window.start_mmddyy, "Start date")
            and self._enter_date(Locators.END_DATE, self.window.end_mmddyy, "End date")
        )

    def submit_search(self):
        self._click(Locators.SEARCH, "Search button")

    # ---------------------------------------------------------
    # Results table
    # ---------------------------------------------------------
    def wait_for_table(self, timeout=None):
        timeout = timeout or self.config.element_timeout
        WebDriverWait(self.driver, timeout,
                      ignored_exceptions=(StaleElementReferenceException,)).until(
            lambda d: any(r.is_displayed() for r in d.find_elements(*Locators.RESULT_ROWS))
        )
        return [r for r in self.driver.find_elements(*Locators.RESULT_ROWS) if r.is_displayed()]

    def wait_for_results(self):
        try:
            WebDriverWait(self.driver, self.config.page_load_timeout).until(
                lambda d: "filingSearchResults.xhtml" in d.current_url
            )
        except TimeoutException:
            logger.warning("Results URL not detected; continuing.")
        time.sleep(5)
        self._scroll_to_bottom()
        time.sleep(2)
        try:
            self.wait_for_table(timeout=self.config.search_result_timeout)
        except TimeoutException:
            self._screenshot("search_results_timeout")
            raise

    def select_rows_per_page(self):
        select_el = WebDriverWait(self.driver, self.config.element_timeout).until(
            EC.presence_of_element_located(Locators.ROWS_PER_PAGE)
        )
        self.driver.execute_script(
            "arguments[0].value = arguments[1];"
            "arguments[0].dispatchEvent(new Event('change', {bubbles:true}));",
            select_el, self.config.rows_per_page,
        )
        self._wait_ajax_idle()
        time.sleep(2)

    def prepare_search(self):
        self.apply_search_filters()
        if not self.enter_dates():
            raise RuntimeError("Date entry failed.")
        self.submit_search()
        self.wait_for_results()
        self.select_rows_per_page()

    def get_page_label(self):
        for element in self.driver.find_elements(*Locators.PAGINATOR_LABEL):
            try:
                text = " ".join(element.text.split())
                if text and "of" in text.lower():
                    return text.strip("() ")
            except WebDriverException:
                pass
        try:
            body_text = self.driver.find_element(By.TAG_NAME, "body").text
            match = re.search(r"\((\d+\s+of\s+\d+)\)", body_text, re.IGNORECASE)
            if match:
                return match.group(1)
        except WebDriverException:
            pass
        return "unknown"

    def _page_numbers(self):
        match = re.search(r"(\d+)\s+of\s+(\d+)", self.get_page_label() or "", re.IGNORECASE)
        if not match:
            return None, None
        return int(match.group(1)), int(match.group(2))

    def _expected_total(self):
        try:
            text = self.driver.find_element(*Locators.TOTAL_FILINGS).text
            return int(re.sub(r"[^\d]", "", text))
        except Exception:
            return None

    def snapshot_page(self, page_no):
        rows = self.wait_for_table(timeout=self.config.page_load_timeout)
        records = []
        for index, row in enumerate(rows, start=1):
            cells = row.find_elements(By.XPATH, "./td")
            values = [" ".join(c.text.split()) for c in cells]
            def col(i):
                return values[i] if len(values) > i else ""
            records.append({
                "page": page_no, "row_number": index,
                "data_rk": row.get_attribute("data-rk"), "cells": values,
                "company_name": col(0), "naic_company_code": col(1),
                "insurance_product_name": col(2), "sub_type_of_insurance": col(3),
                "filing_type": col(4), "filing_status": col(5),
                "serff_tracking_number": col(6), "row_text": " | ".join(values),
            })
        return records

    def _find_next_button(self):
        for element in self.driver.find_elements(*Locators.NEXT_PAGE):
            try:
                classes = element.get_attribute("class") or ""
                if (element.is_displayed() and element.is_enabled()
                        and "ui-state-disabled" not in classes):
                    return element
            except WebDriverException:
                continue
        return None

    def _first_row_key(self):
        rows = self.driver.find_elements(*Locators.RESULT_ROWS)
        if not rows:
            return ""
        return rows[0].get_attribute("data-rk") or rows[0].text

    def _wait_page_change(self, label_before, first_before):
        try:
            WebDriverWait(self.driver, self.config.page_load_timeout,
                          ignored_exceptions=(StaleElementReferenceException,)).until(
                lambda d: self.get_page_label() != label_before
                or (bool(d.find_elements(*Locators.RESULT_ROWS))
                    and self._first_row_key() != first_before)
            )
            return True
        except TimeoutException:
            return False

    def go_to_next_page(self, total_pages=None):
        page_before = self.get_page_label()
        current, detected_total = self._page_numbers()
        total = total_pages if total_pages is not None else detected_total
        if current is not None and total is not None and current >= total:
            return "last"
        next_button = self._find_next_button()
        if next_button is None:
            return "last"
        first_before = self._first_row_key()
        self.driver.execute_script("arguments[0].click();", next_button)
        if not self._wait_page_change(page_before, first_before):
            self._screenshot("next_page_timeout")
            return "stuck"
        time.sleep(1)
        return "moved"

    def _resume_to_page(self, target):
        current, total = self._page_numbers()
        if total is not None and target > total:
            return
        while current is not None and current < target:
            label_before, first_before = self.get_page_label(), self._first_row_key()
            links = [e for e in self.driver.find_elements(*Locators.page_link(target))
                     if e.is_displayed()]
            if links:
                self.driver.execute_script("arguments[0].click();", links[0])
                if not self._wait_page_change(label_before, first_before):
                    raise RuntimeError(f"Could not jump to page {target}.")
            elif self.go_to_next_page(total_pages=total) != "moved":
                raise RuntimeError(f"Could not reach page {target}.")
            new_current, _ = self._page_numbers()
            if new_current is None or new_current <= current:
                raise RuntimeError(f"Paginator did not advance (stuck on page {current}).")
            current = new_current

    # ---------------------------------------------------------
    # Phase 1: collect (Rule 4 applies — refresh session + re-filter)
    # ---------------------------------------------------------
    def _collect_pages(self):
        while True:
            # Rule 3: pre-scrape session-expiry check.
            if self._is_session_expired_page():
                raise SessionExpiredRetry("session expired before scraping page")
            if not self.handle_verification():
                raise RuntimeError("Verification was not completed.")
            self.wait_for_table(timeout=self.config.page_load_timeout)
            current, total_pages = self._page_numbers()
            page_no = current or (self.store.pages_collected + 1)

            if total_pages is not None and current is not None and current > total_pages:
                return True
            if self.store.pages_collected > 0 and page_no <= self.store.pages_collected:
                return True

            records = self.snapshot_page(page_no)
            self.store.add_page(page_no, total_pages, records)
            self._collect_failures = 0
            self._restarts = 0

            if total_pages is not None and page_no >= total_pages:
                return True
            status = self.go_to_next_page(total_pages=total_pages)
            if status == "last":
                return True
            if status == "stuck":
                raise RuntimeError("Next page did not load.")

    def collect_ids(self):
        mode, prepared = "prepare", False
        while not self.store.collection_complete:
            try:
                if mode == "hard":
                    self.restart_session()
                    prepared = False
                if mode in ("prepare", "hard") or not prepared:
                    self.prepare_search()
                    prepared = True
                else:
                    self._refresh()
                    self.wait_for_table(timeout=self.config.page_load_timeout)
                    self.select_rows_per_page()
                self._resume_to_page(self.store.pages_collected + 1)
                if self._collect_pages():
                    expected = self._expected_total()
                    self.store.mark_collection_complete(expected)
                    return True
            except (KeyboardInterrupt, SessionAbort):
                raise
            except SessionExpiredRetry as exc:
                # Rule 4: refresh session on same browser, then re-run filters
                # and resume scraping from pages_collected + 1.
                logger.warning(f"COLLECT: session expired ({exc}); refreshing and resuming.")
                self._collect_failures += 1
                if self._collect_failures >= self.config.max_collect_failures:
                    return False
                try:
                    self.restart_session()
                except SessionAbort:
                    raise
                except Exception:
                    logger.exception("Session refresh failed; will retry.")
                mode = "hard"      # re-run prepare_search next loop iteration
                prepared = False
                time.sleep(self.config.refresh_backoff * self._collect_failures)
            except Exception:
                self._collect_failures += 1
                logger.exception(f"COLLECT failure {self._collect_failures}/{self.config.max_collect_failures}")
                self._screenshot("collect_failure")
                if self._collect_failures >= self.config.max_collect_failures:
                    return False
                problem = self._page_problem()
                if problem == "captcha":
                    mode = "soft" if self.handle_verification() else "hard"
                elif problem in self.HARD_PROBLEMS or self._collect_failures >= 2 or not prepared:
                    mode = "hard"
                else:
                    mode = "soft"
                time.sleep(self.config.refresh_backoff * self._collect_failures)
        return True

    # ---------------------------------------------------------
    # not_down.json (Rule 5)
    # ---------------------------------------------------------
    def _load_not_down_json(self) -> list:
        path = self.config.not_downloaded_json_path
        if not path.is_file():
            return []
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            logger.exception(f"Could not read {path}; starting a fresh list.")
            return []

    def _save_not_down_json(self, entries: list):
        path = self.config.not_downloaded_json_path
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(entries, fh, indent=2, ensure_ascii=False)
        tmp.replace(path)
        logger.info(f"not_down.json updated: {path} ({len(entries)} entry(ies))")

    def _append_not_down(self, entry: dict):
        entries = self._load_not_down_json()
        by_id = {e.get("id"): e for e in entries if e.get("id")}
        by_id[entry["id"]] = entry
        self._save_not_down_json(list(by_id.values()))

    def _export_not_down_xlsx(self):
        try:
            entries = self._load_not_down_json()
            target = self.config.not_downloaded_xlsx_path
            columns = ["Website", "File Url", "Code", "ID",
                       "Tracking Number", "Reason", "Status", "Date"]
            if not entries:
                if not target.exists():
                    pd.DataFrame(columns=columns).to_excel(target, index=False)
                return
            rows = [{
                "Website": e.get("website", ""),
                "File Url": e.get("file_url", ""),
                "Code": e.get("code", ""),
                "ID": e.get("id", ""),
                "Tracking Number": e.get("tracking_number", ""),
                "Reason": e.get("reason", ""),
                "Status": e.get("status", ""),
                "Date": e.get("recorded_at", ""),
            } for e in entries]
            pd.DataFrame(rows, columns=columns).to_excel(target, index=False)
            logger.info(f"Not down.xlsx written: {target} ({len(rows)} row(s))")
        except Exception:
            logger.exception("Could not export Not down.xlsx from not_down.json.")

    # ---------------------------------------------------------
    # Downloads (Rules 4, 5)
    # ---------------------------------------------------------
    def _completed_downloads(self):
        folder = self.config.download_dir
        if not folder.is_dir():
            return set()
        temp_suffixes = (".crdownload", ".part", ".download", ".tmp")
        files = set()
        for path in folder.iterdir():
            if not path.is_file() or path.name.startswith("."):
                continue
            if path.name.lower().endswith(temp_suffixes):
                continue
            files.add(str(path.resolve()))
        return files

    # def _monitor_download(self, baseline, row_number):
    #     wait = self.config.download_wait
    #     started = time.monotonic()
    #     deadline = started + wait
    #     saw_temp = False
    #     folder = self.config.download_dir
    #     folder.mkdir(parents=True, exist_ok=True)
    #     while time.monotonic() < deadline:
    #         in_progress = any(
    #             p.name.lower().endswith((".crdownload", ".part", ".download"))
    #             for p in folder.iterdir() if p.is_file()
    #         )
    #         if in_progress:
    #             saw_temp = True
    #         new_files = sorted(self._completed_downloads() - baseline)
    #         if new_files and not in_progress:
    #             return set(new_files)
    #         time.sleep(0.5)
    #     new_files = sorted(self._completed_downloads() - baseline)
    #     if not new_files:
    #         logger.warning(
    #             f"ROW {row_number}: No completed new file within {wait}s "
    #             f"(saw_temp={saw_temp})."
    #         )
    #     return set(new_files)
    
    def _monitor_download(self, baseline, row_number, early_abort_no_file_after=None):
        wait = self.config.download_wait
        started = time.monotonic()
        deadline = started + wait
        early_deadline = (
            started + early_abort_no_file_after
            if early_abort_no_file_after is not None else None
        )
        saw_temp = False
        folder = self.config.download_dir
        folder.mkdir(parents=True, exist_ok=True)
        last_expired_probe = 0.0

        while time.monotonic() < deadline:
            now = time.monotonic()
            # Fast session-expiry check every 1 s.
            if now - last_expired_probe >= 1.0:
                last_expired_probe = now
                if self._is_session_expired_page():
                    logger.warning(
                        f"ROW {row_number}: session expired while monitoring "
                        f"download — aborting wait."
                    )
                    raise SessionExpiredRetry("session expired during download monitor")

            in_progress = any(
                p.name.lower().endswith((".crdownload", ".part", ".download"))
                for p in folder.iterdir() if p.is_file()
            )
            if in_progress:
                saw_temp = True
            new_files = sorted(self._completed_downloads() - baseline)
            if new_files and not in_progress:
                return set(new_files)

            # Early abort: if nothing has appeared and no download is even
            # started within N seconds, assume the click didn't take.
            if (early_deadline is not None
                    and not saw_temp
                    and not new_files
                    and time.monotonic() >= early_deadline):
                logger.warning(
                    f"ROW {row_number}: no download activity within "
                    f"{early_abort_no_file_after}s of clicking Download — aborting wait."
                )
                return set()

            time.sleep(0.5)

        new_files = sorted(self._completed_downloads() - baseline)
        if not new_files:
            logger.warning(
                f"ROW {row_number}: No completed new file within {wait}s "
                f"(saw_temp={saw_temp})."
            )
        return set(new_files)

    def _move_zip_into_filing_folder(self, zip_paths, code, tracking_num, row_number):
        if not zip_paths:
            return None
        source = max(zip_paths, key=lambda p: Path(p).stat().st_size)
        target = self.config.filing_zip_path(code, tracking_num)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.unlink()
        shutil.move(str(source), str(target))
        for extra in zip_paths:
            if Path(extra).exists():
                try:
                    Path(extra).unlink()
                except OSError:
                    pass
        return target

    def _save_filing_html(self, code, tracking_num, row_number):
        try:
            target = self.config.filing_html_path(code, tracking_num)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(self.driver.page_source, encoding="utf-8")
            return target
        except Exception:
            logger.exception(f"ROW {row_number}: Failed to save filing page HTML.")
            self._screenshot(f"html_save_error_row_{row_number}")
            return None

    def _download_once(self, data_rk, code, row_number):
        url = self.config.filing_url(data_rk)
        self._last_filing_url = url
        self._load_page(url)

        # ---------- post-load checks — CAPTCHA FIRST -----------------------
        # (SERFF sometimes flips to a captcha a moment after the page loads.)
        if self._is_verification_page():
            logger.warning(f"ROW {row_number}: CAPTCHA appeared post-load; solving.")
            if not self.handle_verification():
                raise RuntimeError("Captcha could not be solved.")
            # Re-verify after solving.
            if self._is_verification_page():
                raise RuntimeError("Captcha still present after solving.")

        if self._is_session_expired_page():
            raise SessionExpiredRetry(f"session expired on filing page {url}")

        if self._is_server_error_page():
            raise SessionExpiredRetry(f"HTTP 5xx on filing page {url}")

        try:
            tracking_num = self._read_tracking_number()
        except RuntimeError:
            raise ZipNotAvailable("SERFF Tracking Number not readable")

        record = self.store.records.get(data_rk)
        if record is not None:
            record["tracking_number"] = tracking_num

        self.config.code_dir(code).mkdir(parents=True, exist_ok=True)
        self._save_filing_html(code, tracking_num, row_number)

        if not self.driver.find_elements(*Locators.DOWNLOAD_ZIP):
            self._screenshot(f"no_download_button_row_{row_number}")
            raise ZipNotAvailable("Download ZIP button not present on page")

        # ... rest of the method unchanged


    def _recover_page(self, problem, attempt):
        if problem == "captcha" and attempt == 1 and self.handle_verification():
            return "refreshed"
        if problem in self.HARD_PROBLEMS or attempt >= 2:
            self.restart_session_and_resume(self._last_filing_url)
            return "restarted"
        time.sleep(self.config.refresh_backoff * attempt)
        try:
            self._refresh()
        except WebDriverException:
            self.restart_session_and_resume(self._last_filing_url)
            return "restarted"
        return "refreshed"

    def _download_with_recovery(self, data_rk, code, website):
        row_number = self.store.completed_count + 1
        self._current_site_url = website or self.config.site_home_url
        max_tries = self.config.max_row_retries
        max_session_retries = self.config.max_session_attempts * 3   # independent budget
        reason, terminal = "unknown", False

        attempt = 0                 # real-failure counter
        session_retries = 0         # session-refresh counter (does not consume `attempt`)

        while attempt < max_tries:
            attempt += 1
            logger.info("=" * 70)
            logger.info(
                f"ROW {row_number} | data-rk={data_rk} | code={code} | "
                f"attempt {attempt}/{max_tries} | session_retries {session_retries}/{max_session_retries}"
            )
            try:
                self._download_once(data_rk, code, row_number)
                self.store.mark_completed(data_rk, confirmed=True)
                self._restarts = 0
                return True

            except (KeyboardInterrupt, SessionAbort):
                raise

            except SessionExpiredRetry as exc:
                # Do NOT consume the row's retry budget — refresh session and try again.
                attempt -= 1                    # undo the increment above
                session_retries += 1
                if session_retries > max_session_retries:
                    logger.error(
                        f"ROW {row_number}: session expired {session_retries} times — "
                        f"giving up on this row."
                    )
                    reason = f"SessionExpiredRetry (too many): {exc}"[:300]
                    break
                logger.warning(
                    f"ROW {row_number}: session expired ({exc}); refreshing session "
                    f"and resuming at {self._last_filing_url} "
                    f"(session_retries {session_retries}/{max_session_retries})."
                )
                self._screenshot(f"row_{data_rk}_session_expired")
                try:
                    self.restart_session_and_resume(self._last_filing_url)
                except SessionAbort:
                    raise
                except Exception:
                    logger.exception("Session refresh failed; retrying anyway.")
                # Do NOT increment `attempt`; loop naturally increments it next time
                # only when we re-enter with attempt < max_tries.
                attempt -= 1                     # already undone above; kept for clarity

            except ZipNotAvailable as exc:
                reason = f"ZipNotAvailable: {exc}".strip()[:300]
                self._screenshot(f"row_{data_rk}_no_zip")
                self._cleanup_filing_folder(code, data_rk)
                self.store.mark_skipped(data_rk, reason)
                self._append_not_down({
                    "id": data_rk,
                    "website": website or "",
                    "file_url": self.config.filing_url(data_rk),
                    "code": code or "",
                    "tracking_number": (self.store.records.get(data_rk) or {}).get("tracking_number", ""),
                    "reason": reason,
                    "status": "skipped",
                    "recorded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                })
                terminal = True
                break

            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}".strip()[:300]
                self._screenshot(f"row_{data_rk}_error")
                if attempt >= max_tries:
                    break
                problem = self._page_problem()
                self._recover_page(problem, attempt)

        if not terminal:
            logger.error(
                f"Row data-rk={data_rk} failed (terminal). reason: {reason}"
            )
            self.store.mark_skipped(data_rk, reason)
            self._cleanup_filing_folder(code, data_rk)
            self._append_not_down({
                "id": data_rk,
                "website": website or "",
                "file_url": self.config.filing_url(data_rk),
                "code": code or "",
                "tracking_number": (self.store.records.get(data_rk) or {}).get("tracking_number", ""),
                "reason": reason,
                "status": "failed",
                "recorded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
        return False
    
    def _cleanup_filing_folder(self, code, data_rk):
        record = self.store.records.get(data_rk) or {}
        tracking_num = record.get("tracking_number")
        if not tracking_num:
            return
        zip_path = self.config.filing_zip_path(code, tracking_num)
        if zip_path.is_file():
            try:
                zip_path.unlink()
            except OSError:
                logger.exception(f"Could not remove {zip_path}")

    def download_filings(self, df=None, index=None):
        pending = self.store.pending_keys()
        skipped_count = len(self.store.progress.get("skipped_data_rk") or {})
        total_records = len(self.store.records)

        logger.info("=" * 70)
        logger.info(
            f"DOWNLOAD PHASE: {len(pending)} pending of {total_records} filing id(s); "
            f"completed={self.store.completed_count}, skipped={skipped_count}."
        )
        logger.info("=" * 70)

        if total_records == 0:
            logger.warning("No filing IDs collected for this row; marking the row Partial.")
            return False

        code = ""
        website = ""
        if df is not None and index is not None:
            input_row = df.loc[index]
            code = str(input_row.get("Code", "") or "").strip()
            website = str(input_row.get("Website", "") or "").strip()

        failed = []
        for position, data_rk in enumerate(pending, start=1):
            logger.info(f"DOWNLOAD {position}/{len(pending)}")
            if not self._download_with_recovery(data_rk, code=code, website=website):
                failed.append(data_rk)

        logger.info("=" * 70)
        logger.info(
            f"DOWNLOAD PHASE FINISHED. completed={self.store.completed_count}/{total_records}, "
            f"skipped={skipped_count}, failed_this_run={len(failed)}."
        )
        logger.info("=" * 70)

        # Rule 6: state row is Completed ONLY if every filing got HTML+ZIP.
        all_downloaded = (
            not failed
            and self.store.completed_count == total_records
            and len(self.store.skipped_keys) == 0
        )
        if not all_downloaded:
            logger.warning(
                f"Row will be Partial: completed={self.store.completed_count}/{total_records}, "
                f"skipped={skipped_count}, failed={len(failed)}."
            )
        return all_downloaded

    # ---------------------------------------------------------
    # Excel helpers
    # ---------------------------------------------------------
    def _ensure_excel_columns(self, df):
        for col in ("Status", "Last Comp Date", "File Path", "Code", "Website"):
            if col not in df.columns:
                df[col] = ""
        return df

    def _coerce_object_dtypes(self, df):
        for col in ("Status", "Last Comp Date", "File Path", "Code", "Website"):
            if col in df.columns:
                df[col] = df[col].astype(object)
        return df

    def _all_rows_completed(self, df):
        if df is None or df.empty:
            return False
        return df["Status"].astype(str).str.strip().str.lower().eq("completed").all()

    def _stamp_row(self, df, index, completed):
        window_start_iso = self.window.start_iso
        code = str(df.at[index, "Code"] or "").strip()
        total = len(self.store.records)
        done = self.store.completed_count
        skipped = len(self.store.skipped_keys)
        # Independent derivation (Rule 6): Completed needs every filing on disk.
        completed = bool(completed) and total > 0 and done == total and skipped == 0
        df.at[index, "Status"] = "Completed" if completed else "Partial"
        df.at[index, "Last Comp Date"] = window_start_iso
        df.at[index, "File Path"] = str(self.config.code_dir(code))
        logger.info(
            f"ROW {index + 1} → Status={'Completed' if completed else 'Partial'} "
            f"(completed {done}/{total}, skipped {skipped})."
        )

    def _flush_excel(self, df, index=None):
        try:
            df.to_excel(self.config.input_csv, index=False)   # no encoding kwarg
            if index is None:
                logger.info(f"Urls.xlsx flushed ({len(df)} row(s)).")
            else:
                logger.info(f"Urls.xlsx updated for row {index + 1} "
                            f"(Status={df.at[index, 'Status']}).")
        except Exception:
            logger.exception("Could not flush Urls.xlsx.")

    def _roll_window_and_mark_complete(self, df):
        old = self.window.as_dict()
        new_start, new_end = self.window.roll_back_to_previous_month()
        logger.info(
            f"ALL ROWS COMPLETED. Date window rolled back: "
            f"{old['start_date']}..{old['end_date']} -> "
            f"{new_start.isoformat()}..{new_end.isoformat()}"
        )

    def _reset_progress_for_new_window(self):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        for path in (self.config.table_data_json, self.config.progress_json):
            if path.is_file():
                path.rename(path.with_name(f"{path.name}.done-{stamp}"))

    # ---------------------------------------------------------
    # Main
    # ---------------------------------------------------------
    def run(self):
        df = None
        try:
            logger.info("=" * 70)
            logger.info("SERFF AUTOMATION STARTED")
            logger.info(f"Date window: {self.window.as_dict()}")
            logger.info("=" * 70)

            df = pd.read_excel(self.config.input_csv)
            df.columns = [str(c).strip() for c in df.columns]
            df = self._ensure_excel_columns(df)
            df = self._coerce_object_dtypes(df)

            for index, row in df.iterrows():
                status = str(row.get("Status", "") or "").strip().lower()
                if status == "completed":
                    logger.info(f"Skipping row {index + 1} (already completed).")
                    continue

                code = str(row.get("Code", "") or "").strip()
                url = row["Website"]

                self._make_store(code)
                self._current_site_url = url

                # If IDs are already collected and there's work left to download,
                # we can skip the full search flow: just refresh the session
                # (same browser), then jump straight into the download loop.
                if self.store.collection_complete and self.store.pending_keys():
                    logger.info(
                        f"RESUME MODE: {len(self.store.records)} ids collected, "
                        f"{len(self.store.pending_keys())} still pending — "
                        f"skipping filters and search."
                    )
                    try:
                        if self.driver is None:
                            self.start_browser()
                        self._refresh_session_same_browser()
                    except SessionAbort as exc:
                        logger.error(f"RESUME MODE: session refresh failed: {exc}")
                        self._stamp_row(df, index, False)
                        self._flush_excel(df, index=index)
                        continue
                else:
                    # Fresh start for this state, or all downloads already done:
                    # do the full BEGIN SEARCH -> ACCEPT -> filters -> search flow.
                    self.establish_session_with_retries(url)
                    if not self.store.collection_complete:
                        if not self.collect_ids():
                            logger.warning("Collection stopped early; marking Partial.")
                            self._stamp_row(df, index, False)
                            self._flush_excel(df, index=index)
                            continue

                completed = self.download_filings(df=df, index=index)
                self._stamp_row(df, index, completed)
                self._flush_excel(df, index=index)

            if self._all_rows_completed(df):
                self._roll_window_and_mark_complete(df)
                self._reset_progress_for_new_window()

        except KeyboardInterrupt:
            logger.warning("Automation interrupted by the user.")
        except SessionAbort as exc:
            logger.error(f"Stopping: {exc} State is saved; run again to resume.")
            self._screenshot("session_abort")
        except UnexpectedAlertPresentException:
            logger.exception("Unexpected JavaScript alert interrupted automation.")
            self._screenshot("unexpected_alert")
        except Exception:
            logger.exception("Automation failed.")
            self._screenshot("automation_failure")
        finally:
            self.close()
            try:
                self._export_not_down_xlsx()
            except Exception:
                logger.exception("Could not export Not down.xlsx from not_down.json")
            if df is not None:
                self._flush_excel(df)
        return False


# =============================================================
# ENTRY POINT
# =============================================================

if __name__ == "__main__":
    try:
        Serff().run()
    except DateWindowError as exc:
        logger.error(f"Cannot start: {exc}")
        raise