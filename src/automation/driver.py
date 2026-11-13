import os, subprocess
import platform
import undetected_chromedriver as uc
from pathlib import Path
from src.logging import logger
import os
import subprocess
import platform
from pathlib import Path
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.service import Service as FirefoxService
import undetected_chromedriver as uc

IS_WINDOWS = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"
IS_MAC = platform.system() == "Darwin"


class Browser:
    FIREFOX_INSTALL_DIR: str | None = None

    def __init__(self, headless=False, user_agent=None, window_size=(1920, 1080),
                 incognito=False, proxy=False, download_dir=None,firefox_install_dir: str | None = None,):
        self.headless = headless
        self.user_agent = user_agent
        self.window_size = window_size
        self.driver = None
        self.incognito = incognito
        self.proxy = proxy

        if not download_dir:
            download_dir = Path("Downloads")
        self.download_dir = os.path.abspath(str(download_dir))
        if firefox_install_dir:
            type(self).FIREFOX_INSTALL_DIR = firefox_install_dir

    def get_firefox(self):
        options = FirefoxOptions()

        # ---------- Firefox binary ----------
        firefox_binary = self._firefox_binary()
        if not firefox_binary:
            raise FileNotFoundError(
                "Firefox was not found. Install Firefox or pass "
                "firefox_install_dir= to Browser()."
            )
        options.binary_location = firefox_binary
        logger.info(f"[firefox] Using Firefox binary: {firefox_binary}")

        # ---------- Headless ----------
        if self.headless:
            options.add_argument("-headless")

        # ---------- Window size ----------
        if self.window_size:
            w, h = self.window_size
            options.add_argument(f"--width={w}")
            options.add_argument(f"--height={h}")

        # ---------- User agent ----------
        if self.user_agent:
            options.set_preference("general.useragent.override", self.user_agent)

        # ---------- Preferences ----------
        options.set_preference("dom.webdriver.enabled", False)
        options.set_preference("useAutomationExtension", False)
        options.set_preference("media.navigator.enabled", True)
        options.set_preference("network.http.sendRefererHeader", 2)
        options.set_preference("privacy.resistFingerprinting", False)

        # ---------- Private browsing ----------
        options.add_argument("-private")

        # ---------- undetected-geckodriver ONLY (no fallback) ----------
        install_dir = os.path.dirname(firefox_binary)
        UndetectedFirefox = self._load_undetected_firefox(install_dir)

        if UndetectedFirefox is None:
            raise RuntimeError(
                "undetected-geckodriver is required for Firefox but could not "
                "be imported. Install it with: pip install undetected-geckodriver"
            )

        logger.info("[firefox] Starting undetected Firefox...")
        try:
            driver = UndetectedFirefox(options=options)
        except Exception as exc:
            logger.error(f"[firefox] undetected-geckodriver failed to start: {exc}")
            raise

        self._finalize(driver)
        return driver
    # ---------------- human helpers ---------------- #

    @staticmethod
    def _load_undetected_firefox(firefox_install_dir: str | None):
        """
        Load undetected-geckodriver and register this machine's Firefox
        install dir. The package only ships a Linux entry, so on Windows
        we inject one (mirrors the commented-out block in its constants.py).
        """
        try:
            import undetected_geckodriver.constants as ug_constants
            from undetected_geckodriver import Firefox as UndetectedFirefox
        except Exception as exc:
            raise RuntimeError(
                f"Could not import undetected-geckodriver: {exc}. "
                "Install it with: pip install undetected-geckodriver"
            ) from exc

        params_table = ug_constants.PLATFORM_DEPENDENT_PARAMS
        system = platform.system()

        if system == "Windows" and "Windows" not in params_table:
            local_appdata = os.environ.get(
                "LOCALAPPDATA", str(Path.home() / "AppData" / "Local")
            )
            params_table["Windows"] = {
                "firefox_execs": ["firefox.exe"],
                "firefox_paths": [],
                "undetected_path": os.path.join(local_appdata, "undetected_firefox") + "\\",
                "xul": "xul.dll",
            }

        if system not in params_table:
            raise RuntimeError(f"undetected-geckodriver has no support for {system}")

        if firefox_install_dir:
            paths = params_table[system]["firefox_paths"]
            if firefox_install_dir not in paths:
                paths.insert(0, firefox_install_dir)   # mutate in place

        logger.info(f"[firefox] undetected-geckodriver ready (install dir: {firefox_install_dir})")
        return UndetectedFirefox

    def _firefox_install_dir(self) -> str | None:
        """
        Return the *directory* that contains the Firefox binary.
        undetected-geckodriver wants the install dir, not the exe.
        """
        if self.FIREFOX_INSTALL_DIR and os.path.isdir(self.FIREFOX_INSTALL_DIR):
            return self.FIREFOX_INSTALL_DIR

        candidates: list[str] = []
        if IS_WINDOWS:
            candidates += [
                os.path.expandvars(r"%PROGRAMFILES%\Mozilla Firefox"),
                os.path.expandvars(r"%PROGRAMFILES(X86)%\Mozilla Firefox"),
                os.path.expandvars(r"%LOCALAPPDATA%\Mozilla Firefox"),
            ]
        elif IS_MAC:
            candidates += ["/Applications/Firefox.app/Contents/MacOS"]
        else:
            candidates += [
                "/home/suntec/firefox-mozilla",
                "/opt/firefox",
                "/usr/lib/firefox",
                "/usr/lib64/firefox",
            ]

        for c in candidates:
            exe = os.path.join(c, "firefox.exe" if IS_WINDOWS else "firefox")
            if os.path.isfile(exe):
                return c
        return None

    def _firefox_binary(self) -> str | None:
        install_dir = self._firefox_install_dir()
        if not install_dir:
            return self._which("firefox")
        exe = "firefox.exe" if IS_WINDOWS else "firefox"
        full = os.path.join(install_dir, exe)
        return full if os.path.isfile(full) else None


    def get_chrome(self):
        os.makedirs(self.download_dir, exist_ok=True)

        options = uc.ChromeOptions()
        self._apply_common_chromium_options(options)

        # Launch prefs — read by Chrome before UC's own Preferences.json
        # write happens. Still useful, but the runtime override in
        # Serff.start_browser() is what actually suppresses the Save dialog.
        options.add_experimental_option("prefs", {
            "download.default_directory":        self.download_dir,
            "download.prompt_for_download":      False,
            "download.directory_upgrade":        True,
            "profile.default_content_setting_values.automatic_downloads": 1,
            "safebrowsing.enabled":              False,
            "safebrowsing.disable_download_protection": True,
        })

        version = self._get_chrome_version()
        driver = uc.Chrome(options=options, headless=self.headless, version_main=version)

        # ---- CRITICAL: write prefs AFTER Chrome is up ----
        # UC overwrites Preferences.json at launch, so the launch prefs above
        # are unreliable. Chrome's live prefs are the ones that matter, and
        # they are what these CDP calls set. Both the download folder AND the
        # prompt flag must be set here.
        try:
            driver.execute_cdp_cmd("Page.enable", {})
            # Set the download folder AND behavior via CDP.
            # "allowAndName" is required — "allow" falls back to "default"
            # on many Chromedriver builds, which restores the Save dialog.
            driver.execute_cdp_cmd(
                "Browser.setDownloadBehavior",
                {
                    "behavior": "allowAndName",
                    "downloadPath": self.download_dir,
                    "eventsEnabled": True,
                },
            )
        except Exception:
            # Fallback for older Chromedriver (pre-M107).
            try:
                driver.execute_cdp_cmd(
                    "Page.setDownloadBehavior",
                    {"behavior": "allow", "downloadPath": self.download_dir},
                )
            except Exception:
                pass

        self._finalize(driver)
        return driver

    @staticmethod
    def _get_chrome_version():
        try:
            if platform.system() == "Windows":
                import winreg
                for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                    try:
                        key = winreg.OpenKey(hive, r"Software\Google\Chrome\BLBeacon")
                        return int(winreg.QueryValueEx(key, "version")[0].split(".")[0])
                    except Exception:
                        continue
            elif platform.system() == "Linux":
                for cmd in ("google-chrome", "google-chrome-stable",
                            "chromium-browser", "chromium"):
                    try:
                        r = subprocess.run([cmd, "--version"],
                                           capture_output=True, text=True)
                        if r.returncode == 0:
                            return int(r.stdout.strip().split()[-1].split(".")[0])
                    except Exception:
                        continue
            elif platform.system() == "Darwin":
                try:
                    r = subprocess.run(
                        ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                         "--version"], capture_output=True, text=True)
                    if r.returncode == 0:
                        return int(r.stdout.strip().split()[-1].split(".")[0])
                except Exception:
                    pass
        except Exception:
            pass
        return None

    def _apply_common_chromium_options(self, options):
        if self.window_size:
            w, h = self.window_size
            options.add_argument(f"--window-size={w},{h}")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")

        # Persistent user-data-dir so prefs survive and Chrome doesn't
        # silently reset them to defaults between runs.
        profile_dir = os.path.join(os.getcwd(), ".serff_chrome_profile")
        os.makedirs(profile_dir, exist_ok=True)
        options.add_argument(f"--user-data-dir={profile_dir}")

        if self.incognito:
            options.add_argument("--incognito")
        if self.user_agent:
            options.add_argument(f"--user-agent={self.user_agent}")
        if self.proxy:
            options.add_argument(f"--proxy-server={self.proxy}")

    def _finalize(self, driver):
        if self.window_size and hasattr(driver, "set_window_size"):
            driver.set_window_size(*self.window_size)

    def quit(self):
        if self.driver:
            self.driver.quit()
            self.driver = None
            
if __name__ == "__main__":
    driver = Browser(headless=False).get_chrome()
    driver.get("https://google.com")
    print(driver.title)
    driver.quit()