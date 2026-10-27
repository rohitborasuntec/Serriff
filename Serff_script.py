"""
=============================================================
SERFF FILING ACCESS AUTOMATION  (class based, two phases)
State: Alabama
=============================================================

Place this file at:  src/automation/serff.py

PHASE 1  COLLECT   search -> 100 rows per page -> save every data-rk (filing id)
                   of every results page into serff_table_data.json
PHASE 2  DOWNLOAD  open each filing by URL -> save HTML -> download ZIP

Download folder model (matches the reference script):
    - ONE folder: SerffConfig.download_dir
    - Chrome writes there because driver.py sets download.default_directory
    - The scanner reads the same path via self.config.download_dir
    - No CDP Browser.setDownloadBehavior call — prefs are the single
      source of truth so writing and scanning can never disagree.

Run:
    python3 -m src.automation.serff
"""

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

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
from src.automation.driver import Browser            # <-- ADJUST
from src.logging import logger                       # same import your utils uses
from random import randint


# =============================================================
# EXCEPTIONS
# =============================================================

class SessionAbort(Exception):
    """Too many consecutive failures; stop the run (JSON state is kept for resume)."""


# =============================================================
# CONFIGURATION
# =============================================================

@dataclass
class SerffConfig:
    """All tunable settings in one place."""

    state: str = "AL"
    start_date: str = "01/01/26"
    end_date: str = "10/14/26"

    # Timeouts (seconds)
    element_timeout: int = 30
    page_load_timeout: int = 60
    search_result_timeout: int = 60
    verification_timeout: int = 300

    # Verification: each attempt costs a 2Captcha solve, so cap them.
    max_captcha_attempts: int = 3
    post_solve_wait: int = 3

    # Download monitoring
    download_wait: int = 20
    rows_per_page: str = "100"

    # Retry / recovery
    max_refreshes: int = 3               # driver.refresh() tries for a stuck page
    refresh_backoff: int = 3             # seconds, multiplied by the try number
    max_row_retries: int = 3             # attempts per filing before it is marked failed
    max_collect_failures: int = 6        # consecutive failures while collecting ids
    max_consecutive_restarts: int = 5    # session restarts without any progress in between
    max_session_attempts: int = 3        # tries to log back in when (re)creating a session
    restart_cooldown: int = 10           # seconds, multiplied by the restart number (max x3)

    # Search filters
    business_type_index: int = 2          # option[3]
    insurance_target: str = "LTC06 Long Term Care - Other"

    # Direct filing URL. data-rk of a row == filingId.
    # VERIFY ONCE: click any result row by hand and compare the address bar.
    filing_url_template: str = (
        "https://serff-sfa.naic.org/serff/sfa/search/filingSummary.xhtml?filingId={id}"
    )

    # Paths
    base_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent)

    # Project-local Downloads folder — one source of truth for Chrome
    # (via driver.py prefs) and the scanner (_completed_downloads).
    download_dir: Path = field(
        default_factory=lambda: Path(__file__).resolve().parent / "Downloads"
    )

    @property
    def base_url(self):
        return f"https://serff-sfa.naic.org/serff/sfa/home/{self.state}"

    @property
    def progress_json(self):
        return self.base_dir / "serff_progress.json"

    @property
    def table_data_json(self):
        return self.base_dir / "serff_table_data.json"

    def filing_url(self, data_rk):
        return self.filing_url_template.format(id=data_rk)

    def criteria(self):
        """Identifies the search; saved JSON from a different search is archived."""
        return {
            "state": self.state,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "business_type_index": self.business_type_index,
            "insurance_target": self.insurance_target,
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
    INSURANCE_TRIGGER_FALLBACK = (
        By.XPATH,
        '//*[contains(@class,"ui-selectcheckboxmenu")]'
        '[.//input[contains(@id,"availableTois") or contains(@name,"availableTois")]]'
        '//*[contains(@class,"ui-selectcheckboxmenu-label")]',
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
        return (
            By.XPATH,
            f'//a[contains(@class,"ui-paginator-page") and @aria-label="Page {number}"]',
        )


# =============================================================
# PROGRESS / TABLE DATA STORE
# =============================================================

class ProgressStore:
    """
    Two JSON files:
      table_data  {"meta": {...}, "records": {data_rk: {...}}}   <- phase 1 output
      progress    {"completed_data_rk": [...], "failed": {...}}  <- phase 2 checkpoint
    """

    def __init__(self, progress_path, table_path, criteria):
        self.progress_path = Path(progress_path)
        self.table_path = Path(table_path)
        self.criteria = criteria
        self.progress = self._load_progress()
        self.table = self._load_table()

    # ---------- generic ----------
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

    # ---------- loading ----------
    @staticmethod
    def _new_progress():
        return {"completed_data_rk": [], "completed_count": 0, "unconfirmed_data_rk": [],
                "failed": {}, "updated_at": None}

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
        if data["meta"].get("criteria") != self.criteria:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            logger.warning("Saved ids belong to a different search; archiving old JSON files.")
            for path in (self.table_path, self.progress_path):
                if path.is_file():
                    path.rename(path.with_name(f"{path.name}.stale-{stamp}"))
            self.progress = self._new_progress()
            return self._new_table()
        data.setdefault("records", {})
        return data

    # ---------- phase 1: collection ----------
    @property
    def meta(self):
        return self.table["meta"]

    @property
    def records(self):
        return self.table["records"]

    @property
    def collection_complete(self):
        return bool(self.meta.get("collection_complete"))

    @property
    def pages_collected(self):
        return int(self.meta.get("pages_collected") or 0)

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

    # ---------- phase 2: downloads ----------
    @property
    def completed_keys(self):
        return set(self.progress["completed_data_rk"])

    @property
    def completed_count(self):
        return len(self.progress["completed_data_rk"])

    def pending_keys(self):
        done = self.completed_keys
        return [rk for rk in self.records if rk not in done]

    def mark_completed(self, data_rk, confirmed=True):
        if data_rk not in self.completed_keys:
            self.progress["completed_data_rk"].append(data_rk)
        unconfirmed = self.progress["unconfirmed_data_rk"]
        if not confirmed and data_rk not in unconfirmed:
            unconfirmed.append(data_rk)
        self.progress["failed"].pop(data_rk, None)
        self.progress["completed_count"] = self.completed_count
        self.progress["last_completed_data_rk"] = data_rk
        self.progress["updated_at"] = self._now()
        self._save_json_atomic(self.progress_path, self.progress)

    def mark_failed(self, data_rk, reason):
        entry = self.progress["failed"].setdefault(data_rk, {"runs_failed": 0})
        entry["runs_failed"] += 1
        entry["last_reason"] = reason
        entry["last_row_text"] = self.records.get(data_rk, {}).get("row_text", "")
        self.progress["updated_at"] = self._now()
        self._save_json_atomic(self.progress_path, self.progress)


# =============================================================
# SERFF AUTOMATION
# =============================================================

class Serff:
    """Drives the SERFF State Filing Access site end to end."""

    PROBLEM_MARKERS = {
        "bad_gateway": ("502 bad gateway", "bad gateway", "503 service unavailable",
                        "service unavailable", "504 gateway", "gateway time-out",
                        "gateway timeout"),
        "session_expired": ("session expired", "session has expired", "session timed out",
                            "view expired", "viewexpired"),
        "blocked": ("access is temporarily restricted",),
    }
    HARD_PROBLEMS = {"session_expired", "blocked", "driver_dead"}

    def __init__(self, config=None, browser=None):
        self.config = config or SerffConfig()
        self.browser = browser or Browser(
            headless=False,
            incognito=True,
            download_dir=str(self.config.download_dir),   # same path as prefs & scanner
        )
        self.driver = None
        self.store = ProgressStore(
            self.config.progress_json, self.config.table_data_json, self.config.criteria()
        )
        self._restarts = 0
        self._collect_failures = 0

    # ---------------------------------------------------------
    # Driver lifecycle
    # ---------------------------------------------------------

    def start_browser(self):
        logger.info("Starting Chrome WebDriver.")
        self.driver = self.browser.get_chrome()
        self.driver.set_page_load_timeout(self.config.page_load_timeout)
        # Chrome writes to self.config.download_dir via launch prefs;
        # ensure the folder exists so the first scan sees it.
        self.config.download_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Chrome WebDriver started; downloads -> {self.config.download_dir}")

    def close(self):
        if not self.driver:
            return
        logger.info("Closing Selenium browser.")
        try:
            self.driver.quit()
            logger.info("Browser closed successfully.")
        except Exception:
            logger.exception("Error while closing browser.")
        finally:
            self.driver = None

    # ---------------------------------------------------------
    # Small wrappers around your utils
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
        logger.info(f"Waiting for element: {description}")
        try:
            element = wait_for_element(
                self.driver, locator, timeout=timeout,
                condition="visible", raise_on_timeout=True,
            )
        except TimeoutException:
            logger.error(f"Element not visible within {timeout}s: {description}")
            self._screenshot("element_timeout")
            raise
        logger.info(f"Element visible: {description}")
        return element

    def _click(self, locator, description, timeout=None):
        timeout = timeout or self.config.element_timeout
        logger.info(f"Waiting for clickable element: {description}")
        element = WebDriverWait(self.driver, timeout).until(EC.presence_of_element_located(locator))
        self.driver.execute_script(
            "arguments[0].scrollIntoView({block:'center', behavior:'instant'});", element
        )
        time.sleep(0.5)
        try:
            WebDriverWait(self.driver, timeout).until(EC.element_to_be_clickable(locator)).click()
            logger.info(f"Clicked successfully: {description}")
        except WebDriverException:
            logger.warning(f"Normal click failed for {description}; trying JavaScript click.")
            element = WebDriverWait(self.driver, timeout).until(EC.presence_of_element_located(locator))
            self.driver.execute_script("arguments[0].click();", element)
            logger.info(f"JavaScript click completed: {description}")
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
    # Problem detection + refresh helpers
    # ---------------------------------------------------------
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
            return "driver_dead"

        haystack = f"{title}\n{text}"
        for problem, markers in self.PROBLEM_MARKERS.items():
            if any(marker in haystack for marker in markers):
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
        problem = None
        for attempt in range(self.config.max_refreshes + 1):
            if attempt == 0:
                try:
                    self.driver.get(url)
                except TimeoutException:
                    logger.warning("Navigation timed out. Checking current page.")
            else:
                logger.warning(f"Page problem '{problem}'; refresh {attempt}/{self.config.max_refreshes}.")
                time.sleep(self.config.refresh_backoff * attempt)
                self._refresh()

            time.sleep(1)
            problem = self._page_problem()
            if problem is None:
                return
            if problem == "captcha":
                if self.handle_verification():
                    return
                raise RuntimeError("Captcha could not be solved.")
            if problem in self.HARD_PROBLEMS:
                raise RuntimeError(f"Page problem requiring a new session: {problem}")

        raise RuntimeError(f"Page still broken after {self.config.max_refreshes} refreshes: {problem}")

    def _wait_with_refresh(self, locator, description, condition="visible"):
        tries = self.config.max_refreshes
        for attempt in range(tries + 1):
            try:
                element = wait_for_element(
                    self.driver, locator, timeout=self.config.element_timeout,
                    condition=condition, raise_on_timeout=True,
                )
                logger.info(f"Element ready: {description}")
                return element
            except TimeoutException:
                problem = self._page_problem()
                logger.warning(f"'{description}' not found (page problem: {problem}).")
                self._screenshot("element_timeout")
                if attempt == tries:
                    raise
                if problem == "captcha":
                    if not self.handle_verification():
                        raise RuntimeError("Captcha could not be solved.")
                elif problem in self.HARD_PROBLEMS:
                    raise RuntimeError(f"Page problem requiring a new session: {problem}")
                else:
                    logger.warning(f"Refreshing page (try {attempt + 1}/{tries}).")
                    time.sleep(self.config.refresh_backoff * (attempt + 1))
                    self._refresh()

    # ---------------------------------------------------------
    # Human verification
    # ---------------------------------------------------------
    def _is_verification_page(self):
        try:
            return bool(is_verification_page(self.driver))
        except Exception:
            logger.exception("Could not inspect verification page.")
            return False

    def handle_verification(self):
        if not self._is_verification_page():
            logger.info("No human-verification page detected.")
            return True

        logger.warning("Human verification detected.")
        self._screenshot("human_verification")

        deadline = time.monotonic() + self.config.verification_timeout
        attempts = 0
        while time.monotonic() < deadline and attempts < self.config.max_captcha_attempts:
            attempts += 1
            logger.info(f"Solving verification, attempt {attempts}/{self.config.max_captcha_attempts}.")
            try:
                solve_capctha(self.driver)
            except Exception:
                logger.exception("CAPTCHA solver failed.")
                return False

            time.sleep(self.config.post_solve_wait)
            if not self._is_verification_page():
                logger.info("Verification completed.")
                return True
            logger.warning("Verification is still detected.")

        logger.error("Human verification was not completed.")
        return False

    def _accept_alert(self):
        try:
            alert = WebDriverWait(self.driver, 5).until(EC.alert_is_present())
            logger.info(f"JavaScript alert: {alert.text}")
            alert.accept()
            return True
        except Exception:
            return False

    # ---------------------------------------------------------
    # Download dialog / popup helpers
    # ---------------------------------------------------------
    def _dismiss_blocking_dialogs(self, timeout=5):
        dismissed = False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                alert = WebDriverWait(self.driver, 1).until(EC.alert_is_present())
                logger.warning(f"Dismissing blocking dialog: {alert.text!r}")
                alert.accept()
                dismissed = True
            except TimeoutException:
                break
            except (UnexpectedAlertPresentException, WebDriverException):
                break
        return dismissed

    def _close_extra_windows(self, original_handles):
        try:
            current_handles = set(self.driver.window_handles)
        except WebDriverException:
            return

        extra = current_handles - original_handles
        for handle in extra:
            try:
                self.driver.switch_to.window(handle)
                logger.info(f"Closing popup window opened by download: {handle}")
                self.driver.close()
            except WebDriverException:
                logger.exception(f"Could not close window {handle}.")

        main = original_handles & current_handles
        if not main:
            main = current_handles - extra
        if main:
            try:
                self.driver.switch_to.window(next(iter(main)))
                logger.info("Switched back to the main filing window.")
            except WebDriverException:
                logger.exception("Could not switch back to the main window.")

    # ---------------------------------------------------------
    # Session: home -> BEGIN SEARCH -> ACCEPT
    # ---------------------------------------------------------
    def open_site(self):
        logger.info("STEP 2: Opening SERFF website.")
        self._load_page(self.config.base_url)
        self._log_page_info()
        return self.handle_verification()

    def begin_search(self):
        logger.info("STEP 3: Clicking BEGIN SEARCH.")
        element = self._wait_with_refresh(Locators.BEGIN_SEARCH, "BEGIN SEARCH link")
        old_url = self.driver.current_url
        element.click()
        try:
            WebDriverWait(self.driver, self.config.element_timeout).until(
                lambda d: d.current_url != old_url
            )
        except TimeoutException:
            logger.warning("URL did not change after BEGIN SEARCH.")
        time.sleep(2)
        self._log_page_info()
        return self.handle_verification()

    def accept_terms(self):
        logger.info("STEP 4: Clicking ACCEPT.")
        self._wait_with_refresh(Locators.ACCEPT, "ACCEPT button", condition="presence")
        self._click(Locators.ACCEPT, "ACCEPT button")
        if not self.handle_verification():
            return False
        self._wait_with_refresh(Locators.START_DATE, "Start Submission Date input")
        return True

    def establish_session(self):
        if self.driver is None:
            self.start_browser()
        return self.open_site() and self.begin_search() and self.accept_terms()

    def establish_session_with_retries(self):
        for attempt in range(1, self.config.max_session_attempts + 1):
            try:
                if self.establish_session():
                    return
                logger.error("Session set-up did not finish (verification?).")
            except SessionAbort:
                raise
            except Exception:
                logger.exception(f"Session set-up failed (attempt {attempt}).")
                self._screenshot("session_setup_failure")
            self.close()
            time.sleep(self.config.restart_cooldown * attempt)
        raise SessionAbort("Could not establish a SERFF session.")

    def restart_session(self):
        self._restarts += 1
        if self._restarts > self.config.max_consecutive_restarts:
            raise SessionAbort(
                f"{self.config.max_consecutive_restarts} session restarts in a row without progress."
            )
        logger.warning(f"RESTARTING SESSION ({self._restarts}/{self.config.max_consecutive_restarts}).")
        self.close()
        time.sleep(self.config.restart_cooldown * min(self._restarts, 3))
        self.establish_session_with_retries()

    # ---------------------------------------------------------
    # Search filters
    # ---------------------------------------------------------

    def _select_business_type(self):
        logger.info(f"FILTER: Selecting Business Type option[{self.config.business_type_index + 1}].")
        select_el = WebDriverWait(self.driver, self.config.element_timeout).until(
            EC.presence_of_element_located(Locators.BUSINESS_TYPE)
        )
        Select(select_el).select_by_index(self.config.business_type_index)
        self.driver.execute_script(
            "arguments[0].dispatchEvent(new Event('input', {bubbles:true}));"
            "arguments[0].dispatchEvent(new Event('change', {bubbles:true}));",
            select_el,
        )
        selected = select_el.find_element(
            By.XPATH, f"./option[{self.config.business_type_index + 1}]"
        ).text.strip()
        logger.info(f"FILTER: Business Type selected: {selected}")

        self._wait_ajax_idle()
        time.sleep(randint(1, 3))

    _TICK_UP_TO_JS = """
        var target = arguments[0].trim().toLowerCase();
        var inputs = Array.prototype.slice.call(
            document.querySelectorAll('input[name="simpleSearch:availableTois"]'));
        var idx = -1;
        for (var i = 0; i < inputs.length; i++) {
            if (inputs[i].value.trim().toLowerCase() === target) { idx = i; break; }
        }
        if (idx < 0) { return {found: false, total: inputs.length}; }

        for (var j = 0; j < inputs.length; j++) { inputs[j].checked = (j <= idx); }

        var lis = document.querySelectorAll('.ui-selectcheckboxmenu-items-wrapper ul li');
        for (var k = 0; k < lis.length; k++) {
            var on = (k <= idx);
            var box = lis[k].querySelector('.ui-chkbox-box');
            var icon = box ? box.querySelector('.ui-chkbox-icon') : null;
            if (box) { box.classList.toggle('ui-state-active', on); }
            if (icon) { icon.classList.toggle('ui-icon-check', on); icon.classList.toggle('ui-icon-blank', !on); }
            lis[k].classList.toggle('ui-selectcheckboxmenu-checked', on);
        }

        var w = null;
        try { w = PrimeFaces.getWidgetById('simpleSearch:availableTois'); } catch (e) {}
        if (w) {
            try { if (w.updateLabel) { w.updateLabel(); } } catch (e) {}
            try { if (w.updateToggler) { w.updateToggler(); } } catch (e) {}
            try { if (w.hasBehavior && w.hasBehavior('change')) { w.callBehavior('change'); } } catch (e) {}
        }
        return {found: true, total: inputs.length, index: idx, checked: idx + 1};
    """

    _CLICK_UP_TO_JS = """
        var idx = arguments[0];
        var inputs = document.querySelectorAll('input[name="simpleSearch:availableTois"]');
        var lis = document.querySelectorAll('.ui-selectcheckboxmenu-items-wrapper ul li');
        for (var i = 0; i <= idx && i < lis.length; i++) {
            if (!inputs[i].checked) { lis[i].click(); }
        }
        return document.querySelectorAll('input[name="simpleSearch:availableTois"]:checked').length;
    """

    _COUNT_CHECKED_JS = (
        "return document.querySelectorAll("
        "'input[name=\"simpleSearch:availableTois\"]:checked').length;"
    )

    def _select_insurance_types(self):
        target_text = self.config.insurance_target

        result = self.driver.execute_script(self._TICK_UP_TO_JS, target_text)
        if not result.get("found"):
            logger.error(f"FILTER: Target insurance item '{target_text}' not found "
                         f"({result.get('total')} items on the page).")
            self._screenshot("insurance_target_not_found")
            raise RuntimeError(f"Type of Insurance target not found: {target_text}")

        expected = result["checked"]
        self._wait_ajax_idle()
        actual = self.driver.execute_script(self._COUNT_CHECKED_JS)
        logger.info(f"FILTER: Direct tick selected {actual}/{expected} Type of Insurance items.")

        if actual != expected:
            logger.warning("FILTER: Direct tick did not stick; falling back to widget clicks.")
            self._click(Locators.INSURANCE_TRIGGER, "Type of Insurance dropdown")
            self.driver.execute_script(self._CLICK_UP_TO_JS, result["index"])
            self._wait_ajax_idle(timeout=60)
            actual = self.driver.execute_script(self._COUNT_CHECKED_JS)
            if actual != expected:
                self._screenshot("insurance_selection_mismatch")
                raise RuntimeError(f"Expected {expected} insurance items checked, found {actual}.")
            try:
                self.driver.find_element(By.TAG_NAME, "body").click()
            except WebDriverException:
                pass

        logger.info(f"FILTER: Completed Type of Insurance selection through '{target_text}'.")

    def apply_search_filters(self):
        logger.info("STEP 5: Applying Business Type and Type of Insurance filters.")
        self._select_business_type()
        self._select_insurance_types()

    def _enter_date(self, locator, value, label):
        logger.info(f"Entering {label} {value}.")
        field_el = self._wait_visible(locator, f"{label} input")
        field_el.clear()
        field_el.send_keys(value)
        actual = field_el.get_attribute("value")
        logger.info(f"{label} field value: {actual}")
        if actual != value:
            logger.error(f"{label} validation failed (expected {value}, got {actual}).")
            self._screenshot(f"{label.replace(' ', '_').lower()}_validation")
            return False
        return True

    def enter_dates(self):
        logger.info("STEP 6/7: Entering submission dates.")
        return (
            self._enter_date(Locators.START_DATE, self.config.start_date, "Start date")
            and self._enter_date(Locators.END_DATE, self.config.end_date, "End date")
        )

    def submit_search(self):
        logger.info("STEP 8: Clicking Search.")
        self._click(Locators.SEARCH, "Search button")
        logger.info("Search button clicked successfully.")

    # ---------------------------------------------------------
    # Results table
    # ---------------------------------------------------------
    def wait_for_table(self, timeout=None):
        timeout = timeout or self.config.element_timeout
        logger.info("Waiting for search results table rows.")
        WebDriverWait(
            self.driver, timeout, ignored_exceptions=(StaleElementReferenceException,)
        ).until(
            lambda d: any(r.is_displayed() for r in d.find_elements(*Locators.RESULT_ROWS))
        )
        rows = [r for r in self.driver.find_elements(*Locators.RESULT_ROWS) if r.is_displayed()]
        logger.info(f"Search results table contains {len(rows)} visible row(s).")
        return rows

    def wait_for_results(self):
        logger.info("STEP 9: Waiting for Search Results page to open.")
        try:
            WebDriverWait(self.driver, self.config.page_load_timeout).until(
                lambda d: "filingSearchResults.xhtml" in d.current_url
            )
            logger.info(f"Search Results page opened: {self.driver.current_url}")
        except TimeoutException:
            logger.warning(
                f"Results URL not detected within {self.config.page_load_timeout}s. "
                f"Current URL: {self.driver.current_url}"
            )

        time.sleep(5)
        self._log_page_info()
        self._scroll_to_bottom()
        time.sleep(2)

        try:
            rows = self.wait_for_table(timeout=self.config.search_result_timeout)
        except TimeoutException:
            logger.error("Search results table was not detected.")
            self._log_page_info()
            self._screenshot("search_results_timeout")
            self._scroll_to_bottom()
            time.sleep(2)
            rows = self.wait_for_table(timeout=self.config.search_result_timeout)
            logger.info("Search results table detected on retry.")

        if rows:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center', behavior:'instant'});", rows[0]
            )
            time.sleep(1)
        self._screenshot("search_results_success")
        logger.info("STEP 9 COMPLETED: Table ready.")

    def select_rows_per_page(self):
        logger.info(f"Selecting {self.config.rows_per_page} rows per page.")
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
        WebDriverWait(self.driver, self.config.page_load_timeout).until(
            lambda d: d.find_elements(*Locators.RESULT_ROWS)
        )
        logger.info(f"Rows-per-page dropdown is now: {select_el.get_attribute('value')}")

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
            logger.exception("Could not read paginator label.")
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
                "page": page_no,
                "row_number": index,
                "data_rk": row.get_attribute("data-rk"),
                "cells": values,
                "company_name": col(0),
                "naic_company_code": col(1),
                "insurance_product_name": col(2),
                "sub_type_of_insurance": col(3),
                "filing_type": col(4),
                "filing_status": col(5),
                "serff_tracking_number": col(6),
                "row_text": " | ".join(values),
            })
        return records

    def _find_next_button(self):
        for element in self.driver.find_elements(*Locators.NEXT_PAGE):
            try:
                classes = element.get_attribute("class") or ""
                if element.is_displayed() and element.is_enabled() and "ui-state-disabled" not in classes:
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
            WebDriverWait(
                self.driver, self.config.page_load_timeout,
                ignored_exceptions=(StaleElementReferenceException,),
            ).until(
                lambda d: self.get_page_label() != label_before
                or (bool(d.find_elements(*Locators.RESULT_ROWS)) and self._first_row_key() != first_before)
            )
            return True
        except TimeoutException:
            return False

    def go_to_next_page(self, total_pages=None):
        page_before = self.get_page_label()
        current, detected_total = self._page_numbers()
        total = total_pages if total_pages is not None else detected_total

        if current is not None and total is not None and current >= total:
            logger.info(f"No next page: already on page {current}/{total}.")
            return "last"

        next_button = self._find_next_button()
        if next_button is None:
            logger.info(f"No enabled next-page button. Last page reached ({page_before}).")
            return "last"

        first_before = self._first_row_key()
        logger.info(f"Moving from table page {page_before} to next page.")
        self.driver.execute_script("arguments[0].click();", next_button)

        if not self._wait_page_change(page_before, first_before):
            logger.warning("Page transition was not detected.")
            self._screenshot("next_page_timeout")
            return "stuck"

        time.sleep(1)
        logger.info(f"Now on page label: {self.get_page_label()}")
        return "moved"

    def _resume_to_page(self, target):
        current, total = self._page_numbers()

        if total is not None and target > total:
            logger.info(
                f"Resume target page {target} > total pages {total}; "
                "collection is already at the end."
            )
            return

        while current is not None and current < target:
            label_before, first_before = self.get_page_label(), self._first_row_key()
            links = [e for e in self.driver.find_elements(*Locators.page_link(target)) if e.is_displayed()]
            if links:
                logger.info(f"Jumping to page {target} via paginator link.")
                self.driver.execute_script("arguments[0].click();", links[0])
                if not self._wait_page_change(label_before, first_before):
                    raise RuntimeError(f"Could not jump to page {target}.")
            elif self.go_to_next_page(total_pages=total) != "moved":
                raise RuntimeError(f"Could not reach page {target}.")

            new_current, _ = self._page_numbers()
            if new_current is None or new_current <= current:
                raise RuntimeError(f"Paginator did not advance (stuck on page {current}).")
            current = new_current

        logger.info(f"Positioned on page {current} (wanted {target}).")

    # ---------------------------------------------------------
    # PHASE 1: collect every data-rk into JSON
    # ---------------------------------------------------------
    def _collect_pages(self):
        while True:
            if not self.handle_verification():
                raise RuntimeError("Verification was not completed.")

            self.wait_for_table(timeout=self.config.page_load_timeout)
            current, total_pages = self._page_numbers()
            page_no = current or (self.store.pages_collected + 1)

            if total_pages is not None and current is not None and current > total_pages:
                logger.warning(
                    f"COLLECT: paginator reports page {current} > total {total_pages}; "
                    "nothing further to collect."
                )
                return True

            if self.store.pages_collected > 0 and page_no <= self.store.pages_collected:
                logger.info(
                    f"COLLECT: page {page_no} already collected "
                    f"(saved pages = {self.store.pages_collected}); done."
                )
                return True

            logger.info(f"COLLECT: page {page_no} of {total_pages}")

            records = self.snapshot_page(page_no)
            self.store.add_page(page_no, total_pages, records)
            logger.info(
                f"COLLECT: {len(records)} id(s) from page {page_no}; "
                f"{len(self.store.records)} collected so far."
            )
            self._collect_failures = 0
            self._restarts = 0

            if total_pages is not None and page_no >= total_pages:
                logger.info(f"COLLECT: reached last page ({page_no}/{total_pages}); stopping.")
                return True

            status = self.go_to_next_page(total_pages=total_pages)
            if status == "last":
                return True
            if status == "stuck":
                raise RuntimeError("Next page did not load.")

    def collect_ids(self):
        mode = "prepare"
        prepared = False

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
                    found = len(self.store.records)
                    if expected is not None and expected != found:
                        logger.warning(f"COLLECT: site reports {expected} filings but {found} ids were saved.")
                    logger.info(f"COLLECT COMPLETE: {found} filing id(s) saved.")
                    return True

            except (KeyboardInterrupt, SessionAbort):
                raise
            except Exception:
                self._collect_failures += 1
                logger.exception(f"COLLECT failure {self._collect_failures}/{self.config.max_collect_failures}")
                self._screenshot("collect_failure")
                if self._collect_failures >= self.config.max_collect_failures:
                    logger.error("Too many consecutive failures while collecting ids.")
                    return False

                problem = self._page_problem()
                logger.warning(f"COLLECT: page problem = {problem}")
                if problem == "captcha":
                    mode = "soft" if self.handle_verification() else "hard"
                elif problem in self.HARD_PROBLEMS or self._collect_failures >= 2 or not prepared:
                    mode = "hard"
                else:
                    mode = "soft"
                time.sleep(self.config.refresh_backoff * self._collect_failures)
        return True

    # ---------------------------------------------------------
    # Downloads
    # ---------------------------------------------------------
    def _completed_downloads(self):
        """Return completed files in self.config.download_dir (same filter as reference)."""
        folder = self.config.download_dir
        if not folder.is_dir():
            logger.warning(f"Download directory does not exist: {folder}")
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

    def _monitor_download(self, baseline, row_number):
        """Watch the download folder; return early once a completed ZIP appears."""
        wait = self.config.download_wait
        started = time.monotonic()
        deadline = started + wait
        last_logged = -1
        saw_temp = False

        folder = self.config.download_dir
        folder.mkdir(parents=True, exist_ok=True)

        logger.info(f"ROW {row_number}: Monitoring ZIP download (max {wait}s).")
        logger.info(f"Download directory: {folder}")

        while time.monotonic() < deadline:
            elapsed = int(time.monotonic() - started)

            in_progress = any(
                p.name.lower().endswith((".crdownload", ".part", ".download"))
                for p in folder.iterdir() if p.is_file()
            )
            if in_progress:
                saw_temp = True

            new_files = sorted(self._completed_downloads() - baseline)
            if new_files and not in_progress:
                for path in new_files:
                    logger.info(f"ROW {row_number}: New completed download: {path}")
                return True

            if elapsed % 5 == 0 and elapsed != last_logged:
                logger.info(
                    f"ROW {row_number}: {elapsed}/{wait}s elapsed; "
                    f"{len(new_files)} new completed file(s); in-progress={in_progress}"
                )
                last_logged = elapsed

            time.sleep(0.5)

        new_files = sorted(self._completed_downloads() - baseline)
        if not new_files:
            logger.warning(
                f"ROW {row_number}: No completed new file within {wait}s "
                f"(saw_temp={saw_temp}); the ZIP may still be downloading."
            )
        else:
            for path in new_files:
                logger.info(f"ROW {row_number}: New completed download: {path}")
        return bool(new_files)

    def _save_filing_html(self, data_rk, row_number):
        try:
            safe_rk = re.sub(r"[^A-Za-z0-9_-]+", "_", str(data_rk or "unknown"))
            name = f"filing_page_row_{row_number}_data-rk_{safe_rk}"
            save_html(self.driver.page_source, name)
        except Exception:
            logger.exception(f"ROW {row_number}: Failed to save filing page HTML.")
            self._screenshot(f"html_save_error_row_{row_number}")

    def _download_once(self, data_rk, row_number):
        url = self.config.filing_url(data_rk)
        logger.info(f"ROW {row_number}: Opening filing {data_rk}: {url}")

        self._load_page(url)
        self._wait_with_refresh(Locators.DOWNLOAD_ZIP, "Download Zip File button", condition="presence")
        time.sleep(2)
        self._log_page_info()

        self._save_filing_html(data_rk, row_number)

        baseline = self._completed_downloads()
        original_handles = set(self.driver.window_handles)

        self._click(Locators.DOWNLOAD_ZIP, "Download Zip File button")

        self._dismiss_blocking_dialogs(timeout=5)
        self._close_extra_windows(original_handles)
        self._dismiss_blocking_dialogs(timeout=2)

        return self._monitor_download(baseline, row_number)

    def _recover_page(self, problem, attempt):
        if problem == "captcha" and attempt == 1 and self.handle_verification():
            return "refreshed"
        if problem in self.HARD_PROBLEMS or attempt >= 2:
            self.restart_session()
            return "restarted"
        time.sleep(self.config.refresh_backoff * attempt)
        try:
            self._refresh()
        except WebDriverException:
            self.restart_session()
            return "restarted"
        return "refreshed"

    def _download_with_recovery(self, data_rk):
        row_number = self.store.completed_count + 1
        max_tries = self.config.max_row_retries
        reason = "unknown"

        for attempt in range(1, max_tries + 1):
            logger.info("=" * 70)
            logger.info(f"ROW {row_number} | data-rk={data_rk} | attempt {attempt}/{max_tries}")
            logger.info(f"ROW DATA: {self.store.records.get(data_rk, {}).get('row_text', '')}")
            try:
                confirmed = self._download_once(data_rk, row_number)
                self.store.mark_completed(data_rk, confirmed=confirmed)
                self._restarts = 0
                logger.info(f"ROW COMPLETE. Total completed overall: {self.store.completed_count}")
                return True
            except (KeyboardInterrupt, SessionAbort):
                raise
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}".strip()[:300]
                logger.exception(f"Row failed for data-rk={data_rk}")
                self._screenshot(f"row_{data_rk}_error")
                if attempt == max_tries:
                    break
                problem = self._page_problem()
                logger.warning(f"Recovering (page problem = {problem}).")
                self._recover_page(problem, attempt)

        logger.error(f"Row data-rk={data_rk} failed {max_tries} times; marked failed, moving on.")
        self.store.mark_failed(data_rk, reason)
        return False

    def download_filings(self):
        pending = self.store.pending_keys()
        logger.info("=" * 70)
        logger.info(f"DOWNLOAD PHASE: {len(pending)} pending of {len(self.store.records)} filing id(s).")
        logger.info("=" * 70)

        failed = []
        for position, data_rk in enumerate(pending, start=1):
            logger.info(f"DOWNLOAD {position}/{len(pending)}")
            if not self._download_with_recovery(data_rk):
                failed.append(data_rk)

        logger.info("=" * 70)
        logger.info(f"DOWNLOAD PHASE FINISHED. Completed overall: {self.store.completed_count}; "
                    f"failed this run: {len(failed)}")
        if failed:
            logger.warning(f"Failed ids (retried automatically on the next run): {failed}")
        logger.info("=" * 70)
        return not failed

    # ---------------------------------------------------------
    # Main entry
    # ---------------------------------------------------------
    def run(self):
        try:
            logger.info("=" * 70)
            logger.info("SERFF AUTOMATION STARTED")
            logger.info("=" * 70)

            self.establish_session_with_retries()

            if self.store.collection_complete:
                logger.info(
                    f"Ids already collected ({len(self.store.records)}); "
                    f"{len(self.store.pending_keys())} still to download. "
                    "Skipping filters and search."
                )
            else:
                logger.info(
                    f"PHASE 1: collecting ids (pages already saved: {self.store.pages_collected})."
                )
                if not self.collect_ids():
                    logger.warning("Collection stopped early; run again to resume.")
                    return False

            logger.info("PHASE 2: downloading filings.")
            completed = self.download_filings()
            if completed:
                logger.info("All filings processed.")
            else:
                logger.warning("Some filings failed; run again to retry them.")
            return completed

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
        return False


# =============================================================
# ENTRY POINT
# =============================================================

if __name__ == "__main__":
    Serff().run()