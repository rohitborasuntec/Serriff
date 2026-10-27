import json,os
import time,random
from twocaptcha import TwoCaptcha
from datetime import datetime
from src.logging import logger
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
import re
import time
from datetime import datetime, date
from random import randint
import random,re
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

def solve_capctha(driver):
    api_key = api_key or os.environ.get('APIKEY_2CAPTCHA')
    if not api_key:
        raise ValueError(
            "2Captcha API key missing. Pass api_key= or set "
            "APIKEY_2CAPTCHA env var."
        )

    solver = TwoCaptcha(api_key)
    user_agent = driver.execute_script("return navigator.userAgent;")
  
    props = None
    for _ in range(20):
        props = driver.execute_script("return window.gokuProps || null;")
        if props:
            break
        time.sleep(random.randint(0.2,1))

    if not props:
        raise RuntimeError("No gokuProps found: no WAF challenge on this page")

    siteKey = props["key"]      
    iv = props["iv"]
    context = props["context"]

    try:
    # Call the amazon_waf solver method passing the active User-Agent
        result = solver.amazon_waf(
            sitekey=siteKey,  
            iv=iv,                
            context=context,
            url=driver.current_url,
            userAgent=user_agent  # Crucial: Keeps your fingerprint synchronized
        )

        print("CAPTCHA Solved Successfully!")
        print("Result Code / Token:", result)

    except Exception as e:
        print(f"Error occurred while solving CAPTCHA: {e}")
        driver.quit()
        raise e

    # Parse out the solution payload
    data = result["code"]
    if isinstance(data, str):
        data = json.loads(data)

    # Re-map 'existing_token' to 'aws-waf-token'
    cookies_to_inject = dict(data)
    if "existing_token" in cookies_to_inject:
        cookies_to_inject["aws-waf-token"] = cookies_to_inject.pop("existing_token")

    # Target base domain for cookie scoping
    cookie_domain = "serff-sfa.naic.org"

    # Loop and safely attach formatted cookies to Selenium
    for name, value in cookies_to_inject.items():
        try:
            driver.add_cookie({
                "name": name,
                "value": str(value),
                "domain": cookie_domain,
                "path": "/"
            })
        except Exception as cookie_err:
            print(f"Skipping cookie injection payload error for {name}: {cookie_err}")

    # Refresh the session to submit tokens to the WAF layer
    driver.refresh()

def handle_verification(driver):
    """Wait for manual verification if required."""

    if not is_verification_page(driver):
        logger.info(
            "No human-verification page detected."
        )
        return True

    logger.warning(
        "Human verification detected. Complete it manually in Chrome."
    )

    save_screenshot(
        driver,
        "human_verification"
    )
    VERIFICATION_TIMEOUT = 300

    deadline = (
        time.monotonic()
        + VERIFICATION_TIMEOUT
    )

    while time.monotonic() < deadline:

        solve_capctha(driver)

        time.sleep(2)

        if not is_verification_page(driver):
            logger.info(
                "Verification completed."
            )
            return True

        logger.warning(
            "Verification is still detected."
        )

    logger.error(
        "Human-verification timeout reached."
    )

    return False


def is_verification_page(driver):
    """Detect a possible human-verification page."""

    # try:
    title = driver.title.lower()
    url = driver.current_url.lower()
    source = driver.page_source.lower()

    return (
        "human verification" in title
        or (
            "useragreement.xhtml" in url
            and (
                "let's confirm you are human" in source
                or "complete the security check" in source
            )
        )
    )

    # except WebDriverException:
    #     logger.exception(
    #         "Could not inspect verification page."
    #     )
    #     return False

def check_for_bot(driver):
    acc_den_xpath = '//*[contains(.,"Access is temporarily restricted")]'
    if driver.find_elements(By.XPATH, acc_den_xpath):
        # breakpoint()
        return True
    return False

def wait_for_element(driver, locator, timeout=5, condition="visible", poll=0.5, raise_on_timeout=False):
    conditions = {
        "presence":  EC.presence_of_element_located,
        "visible":   EC.visibility_of_element_located,
        "clickable": EC.element_to_be_clickable,
        "invisible": EC.invisibility_of_element_located,
    }
    if condition not in conditions:
        raise ValueError(f"Unknown condition: {condition}")
    wait = WebDriverWait(driver, timeout, poll_frequency=poll)
    try:
        return wait.until(conditions[condition](locator))
    except TimeoutException:
        if raise_on_timeout:
            raise
        return None

def save_html(html_text, file_name="Test"):
    folder_path = Path("HTML")
    folder_path.mkdir(parents=True, exist_ok=True)

    stem = Path(file_name).stem
    # Sanitize: replace Windows-illegal chars, strip trailing dots/spaces
    stem = re.sub(r'[<>:"/\\|?*]', '_', stem).rstrip('. ')
    if not stem:
        stem = "output"
    # Optional: cap length to stay under Windows MAX_PATH
    stem = stem[:150]

    today = date.today().strftime("%Y-%m-%d")
    final_name = f"{stem}_{today}.html"
    file_path = folder_path / final_name
    file_path.write_text(html_text, encoding="utf-8")
    logger.info(f"--------HTML SAVED for {file_path}----------")


def save_screenshot(driver, file_name="Test"):
    folder_path = Path("Screenshots")
    folder_path.mkdir(parents=True, exist_ok=True)

    stem = Path(file_name).stem
    stem = re.sub(r'[<>:"/\\|?*]', '_', stem).rstrip('. ')
    if not stem:
        stem = "output"
    stem = stem[:150]

    # Generate the filename using today's date
    today = date.today().strftime("%Y-%m-%d")
    final_name = f"{stem}_{today}.png"
    file_path = folder_path / final_name

    success = driver.save_screenshot(str(file_path))

    if success:
        logger.info(f"-------- SS SAVED: {file_path} ----------")
        return str(file_path)

    logger.error(f"Failed to save screenshot: {file_path}")
    return None
