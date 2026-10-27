import os, subprocess
import platform
import undetected_chromedriver as uc
from pathlib import Path


import os
import subprocess
import platform
from pathlib import Path

import undetected_chromedriver as uc


class Browser:

    def __init__(self, headless=False, user_agent=None, window_size=(1920, 1080),
                 incognito=False, proxy=False, download_dir=None):
        self.headless = headless
        self.user_agent = user_agent
        self.window_size = window_size
        self.driver = None
        self.incognito = incognito
        self.proxy = proxy

        if not download_dir:
            download_dir = Path("Downloads")
        self.download_dir = os.path.abspath(str(download_dir))

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