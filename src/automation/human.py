"""
src/automation/human.py

Human-like interaction helpers for Selenium.
Usage:
    self.human = Human(driver)          # after the driver is created
    self.human.click(element)           # instead of element.click()
    self.human.type_text(el, "01/05/26")
    self.human.pause(1, 3)              # instead of time.sleep(random)
    self.human.between_items()          # pacing between filings
"""
import random
import time

from selenium.common.exceptions import (
    MoveTargetOutOfBoundsException,
    WebDriverException,
)
from selenium.webdriver import ActionChains
from selenium.webdriver.common.keys import Keys


class Human:
    def __init__(self, driver=None, speed: float = 1.0,
                 long_break_every=(12, 25), long_break_secs=(20, 60)):
        """
        speed: >1 = slower overall (multiplies every delay).
        long_break_every: a long "coffee break" after N items (N random in range).
        """
        self.driver = driver
        self.speed = speed
        self.long_break_every = long_break_every
        self.long_break_secs = long_break_secs
        self._items = 0
        self._next_break = random.randint(*long_break_every)

    def bind(self, driver):
        self.driver = driver

    # ----------------------------------------------------------
    # Timing
    # ----------------------------------------------------------
    def pause(self, lo=0.6, hi=2.2):
        """Skewed random delay (most waits short, a few long) + rare distraction."""
        delay = lo + (hi - lo) * random.betavariate(2, 5)
        if random.random() < 0.03:                      # occasional distraction
            delay += random.uniform(2, 5)
        time.sleep(delay * self.speed)

    def dwell(self, lo=1.5, hi=4.0, scroll=True):
        """Look at a freshly loaded page like a person reading it."""
        self.pause(lo, hi)
        if scroll and random.random() < 0.6:
            self.scroll_reading(max_px=random.randint(300, 900))

    def between_items(self, base=(3.0, 9.0)):
        """Pacing between filings; periodic longer breaks."""
        self._items += 1
        self.pause(*base)
        if self._items >= self._next_break:
            secs = random.uniform(*self.long_break_secs) * self.speed
            time.sleep(secs)
            self._items = 0
            self._next_break = random.randint(*self.long_break_every)

    # ----------------------------------------------------------
    # Scrolling
    # ----------------------------------------------------------
    def scroll_reading(self, max_px=800):
        """Scroll down in uneven steps, sometimes back up a little."""
        done = 0
        try:
            while done < max_px:
                step = random.randint(120, 380)
                self.driver.execute_script("window.scrollBy(0, arguments[0]);", step)
                done += step
                time.sleep(random.uniform(0.15, 0.7) * self.speed)
                if random.random() < 0.15:
                    back = random.randint(40, 150)
                    self.driver.execute_script("window.scrollBy(0, -arguments[0]);", back)
                    time.sleep(random.uniform(0.2, 0.5) * self.speed)
        except WebDriverException:
            pass

    def scroll_into_view(self, element):
        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center', behavior:'smooth'});",
                element,
            )
        except WebDriverException:
            return
        time.sleep(random.uniform(0.4, 0.9) * self.speed)

    # ----------------------------------------------------------
    # Mouse
    # ----------------------------------------------------------
    def _offsets_in(self, element):
        """Random point inside the element (relative to its centre)."""
        try:
            size = element.size
            w, h = max(size.get("width", 10), 4), max(size.get("height", 10), 4)
        except WebDriverException:
            w, h = 20, 10
        dx = int(random.uniform(-0.3, 0.3) * w)
        dy = int(random.uniform(-0.3, 0.3) * h)
        return dx, dy

    def move_to(self, element):
        """Approach the element through 1-2 waypoints, ending inside it."""
        dx, dy = self._offsets_in(element)
        try:
            actions = ActionChains(self.driver)
            for scale in (random.randint(40, 120), random.randint(10, 30)):
                actions.move_to_element_with_offset(
                    element, dx + random.randint(-scale, scale), dy + random.randint(-scale, scale)
                ).pause(random.uniform(0.03, 0.15))
            actions.move_to_element_with_offset(element, dx, dy)
            actions.perform()
            return dx, dy
        except (MoveTargetOutOfBoundsException, WebDriverException):
            # Waypoints can fall outside the viewport; go straight to the element.
            try:
                ActionChains(self.driver).move_to_element(element).perform()
            except WebDriverException:
                pass
            return 0, 0

    def click(self, element):
        """Scroll, hover, hesitate, click. Falls back to JS click."""
        self.scroll_into_view(element)
        dx, dy = self.move_to(element)
        time.sleep(random.uniform(0.15, 0.6) * self.speed)
        try:
            ActionChains(self.driver).move_to_element_with_offset(element, dx, dy) \
                .pause(random.uniform(0.04, 0.14)).click().perform()
        except WebDriverException:
            self.driver.execute_script("arguments[0].click();", element)
        time.sleep(random.uniform(0.2, 0.7) * self.speed)

    def hover(self, element):
        self.scroll_into_view(element)
        self.move_to(element)
        time.sleep(random.uniform(0.2, 0.8) * self.speed)

    def wiggle(self):
        """Tiny idle mouse movement."""
        try:
            ActionChains(self.driver).move_by_offset(
                random.randint(-6, 6), random.randint(-6, 6)
            ).perform()
        except WebDriverException:
            pass

    # ----------------------------------------------------------
    # Typing
    # ----------------------------------------------------------
    def type_text(self, element, text, clear=True):
        """Focus the field, clear it with keys, type char by char with jitter."""
        self.click(element)
        if clear:
            element.send_keys(Keys.CONTROL, "a")
            time.sleep(random.uniform(0.05, 0.2))
            element.send_keys(Keys.BACKSPACE)
            time.sleep(random.uniform(0.1, 0.3))
        for ch in str(text):
            element.send_keys(ch)
            delay = random.uniform(0.06, 0.22)
            if random.random() < 0.08:
                delay += random.uniform(0.25, 0.7)
            time.sleep(delay * self.speed)
        time.sleep(random.uniform(0.2, 0.6) * self.speed)