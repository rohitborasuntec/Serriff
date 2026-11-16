import os
import platform
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()
# SeleniumBase provides UC mode and proxy handling.
from seleniumbase import Driver
# Note: We no longer import Selenium Wire or undetected_chromedriver directly.
# SeleniumBase bundles its own optimized version of undetected-chromedriver.

IS_WINDOWS = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"
IS_MAC = platform.system() == "Darwin"


class Browser:
    FIREFOX_INSTALL_DIR: str | None = None

    def __init__(
        self,
        headless=False,
        user_agent=None,
        window_size=(1920, 1080),
        incognito=False,
        proxy=False,                     # Can be a dict (seleniumwire style) or a string
        download_dir=None,
        firefox_install_dir: str | None = None,
    ):
        self.headless = headless
        self.user_agent = user_agent
        self.window_size = window_size
        self.driver = None
        self.incognito = incognito
        self.proxy = proxy or os.environ.get('PROXY')

        if not download_dir:
            download_dir = Path("Downloads")
        self.download_dir = os.path.abspath(str(download_dir))
        
        if firefox_install_dir:
            type(self).FIREFOX_INSTALL_DIR = firefox_install_dir

    # ------------------------------------------------------------------ #
    # CHROME (with SeleniumBase UC Mode + Proxy)
    # ------------------------------------------------------------------ #
    def get_chrome(self):
        """
        Returns an initialized SeleniumBase WebDriver.
        """
        os.makedirs(self.download_dir, exist_ok=True)

        driver_kwargs = {
            "uc": True,                                      # Enable Undetected Chrome
            "headless": self.headless,
            "window_size": f"{self.window_size[0]},{self.window_size[1]}",
            "incognito": self.incognito,
        }

        # 2. Proxy configuration
        if self.proxy:
            # SeleniumBase expects a string format: "user:pass@host:port"
            # If you are passing the old seleniumwire dict format, we try to extract the URL
            if isinstance(self.proxy, dict):
                proxy_str = self.proxy.get("proxy", {}).get("http") or str(self.proxy)
            else:
                proxy_str = str(self.proxy)

            proxy_str = proxy_str.strip()
            # SeleniumBase expects auth as user:password@host:port, not the
            # common URL form http://user:password@host:port/. Move any URL
            # scheme after the credentials so SeleniumBase parses auth correctly.
            if "://" in proxy_str:
                scheme, address = proxy_str.split("://", 1)
                if scheme.lower() in {"http", "https", "socks4", "socks5", "socks5h"} and "@" in address:
                    credentials, server = address.rsplit("@", 1)
                    proxy_str = f"{credentials}@{scheme.lower()}://{server}"
            proxy_str = proxy_str.rstrip("/")
            if not proxy_str:
                raise ValueError("Proxy is empty after removing whitespace and trailing slashes.")
            driver_kwargs["proxy"] = proxy_str
            if "@" in proxy_str:
                # With Chrome 152, SeleniumBase UC mode launches but does not
                # answer the proxy's auth challenge. Standard Chrome correctly
                # loads SeleniumBase's proxy-auth extension.
                driver_kwargs["uc"] = False
                print("[chrome] Authenticated proxy detected; using standard Chrome for proxy authentication.")
            display_proxy = (
                f"***@{proxy_str.rsplit('@', 1)[1]}"
                if "@" in proxy_str else proxy_str
            )
            print(f"[chrome] Using proxy: {display_proxy}")

        # 3. User Agent
        if self.user_agent:
            driver_kwargs["agent"] = self.user_agent

        # SeleniumBase's public Driver factory doesn't accept a downloads_path
        # option, but its launcher reads this value when configuring Chrome prefs.
        # Set it only during synchronous driver creation so Chrome writes directly
        # to the same folder the application scans.
        from seleniumbase.core import browser_launcher

        original_downloads_folder = browser_launcher.DOWNLOADS_FOLDER
        browser_launcher.DOWNLOADS_FOLDER = self.download_dir
        try:
            self.driver = Driver(**driver_kwargs)
        finally:
            browser_launcher.DOWNLOADS_FOLDER = original_downloads_folder

        return self.driver

    # ------------------------------------------------------------------ #
    # FIREFOX 
    # ------------------------------------------------------------------ #
    def get_firefox(self):
        # ... (original implementation remains exactly the same) ...
        pass

    # ------------------------------------------------------------------ #
    # CLEANUP
    # ------------------------------------------------------------------ #
    def quit(self):
        """
        Close the active WebDriver, if any.
        """
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None


# ---------------------------------------------------------------------- #
# Example usage
# ---------------------------------------------------------------------- #
if __name__ == "__main__":
    # Example 1: Simple proxy (HTTP/HTTPS with auth)
    proxy_url = "username:password@proxy.example.com:8080"
    # Example 2: SOCKS5 proxy
    # proxy_url = "socks5://username:password@127.0.0.1:9050"

    browser = Browser(headless=False, proxy=proxy_url)

    # Verify the proxy IP using the initialized WebDriver.
    driver = browser.get_chrome()
    try:
        driver.get("https://httpbin.org/ip")
        print("Page title:", driver.title)
        print("\n--- Page Text (IP Check) ---")
        print(driver.find_element("tag name", "body").text)
    finally:
        browser.quit()