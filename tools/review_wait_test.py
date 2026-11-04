"""Prove a real Chrome can sit waiting for a human, without touching LinkedIn.

Opens a throwaway Chrome profile on a local page shaped like a post, types a draft
into its box, and runs the review run's real wait loop for ``--minutes``. Then the
page itself edits the text and "clicks" Comment, and the wait must report that
edited text. It also checks that a box left with text does not block the next page
load the way a "Leave site?" prompt would.

Never opens LinkedIn and never touches a profile's session. Ends with a verdict:
HEALTHY (exit 0) or FAILED (exit 1).

    python tools/review_wait_test.py --minutes 20
"""

import argparse
import logging
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

from linkedin_automation import profile_manager as pm
from linkedin_automation import review_run as rr
from linkedin_automation.comment_poster import LinkedInCommentPoster

DRAFT = "A draft comment typed by the wait test, long enough to find again."
EDITED = "The wording after the page edited it, standing in for a human edit."

PAGE = """<!doctype html><html><body>
<div data-testid="x-commentList">Someone 2h ago An earlier comment</div>
<div role="textbox" contenteditable="true" id="box" style="border:1px solid"></div>
<button id="submit">Comment</button>
<script>
const box = document.getElementById('box');
const list = document.querySelector("[data-testid='x-commentList']");
document.getElementById('submit').onclick = () => {
  list.textContent += ' You now ' + box.innerText;
  box.innerText = '';
};
window.addEventListener('beforeunload', e => {
  if (box.innerText.trim()) { e.preventDefault(); e.returnValue = ''; }
});
window.humanActs = () => {
  box.innerText = %r;
  setTimeout(() => document.getElementById('submit').click(), 3000);
};
</script></body></html>""" % EDITED


class _ActsAfter(rr.PageReader):
    """The real page reader, plus the page's stand-in human at the set time."""

    def __init__(self, poster, url, act_at):
        super().__init__(poster, url)
        self.act_at = act_at
        self.acted = False
        self.looks = 0

    def box_text(self):
        self.looks += 1
        if not self.acted and time.time() >= self.act_at:
            self.acted = True
            self.poster.driver.execute_script("window.humanActs();")
        return super().box_text()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--minutes", type=float, default=20)
    parser.add_argument("--visible", action="store_true", help="Show the Chrome window")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)

    workdir = tempfile.mkdtemp(prefix="review_wait_test_")
    page_path = os.path.join(workdir, "post.html")
    with open(page_path, "w", encoding="utf-8") as f:
        f.write(PAGE)
    url = "file:///" + page_path.replace("\\", "/")

    options = Options()
    options.add_argument(f"--user-data-dir={os.path.join(workdir, 'chrome')}")
    if not args.visible:
        options.add_argument("--headless=new")
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()),
                              options=options)
    # The same limit the app sets, so a blocked page load shows up as it would there.
    driver.set_page_load_timeout(pm.PAGE_LOAD_TIMEOUT_SECONDS)
    failures = []
    started = time.time()
    try:
        driver.get(url)
        poster = LinkedInCommentPoster.__new__(LinkedInCommentPoster)
        poster.driver = driver
        poster.logger = logging.getLogger("review_wait_test")
        poster.profile_name = "review_wait_test"
        poster.human_click_only = True

        driver.find_element("id", "box").send_keys(DRAFT)

        reader = _ActsAfter(poster, url, started + args.minutes * 60)
        result = rr.wait_for_human(reader, rr.ReviewControl(), DRAFT,
                                   poll_seconds=0.75, confirm_seconds=20)
        waited = (time.time() - started) / 60
        print(f"waited {waited:.1f} min, {reader.looks} looks at the page: {result}")
        if result.get("outcome") != "posted":
            failures.append(f"wait ended with {result}")
        elif result.get("final_text") != EDITED:
            failures.append(f"final text was {result.get('final_text')!r}")
        if waited < args.minutes:
            failures.append("the wait ended before the human acted")

        # Leaving a page: the run empties the box first, so no prompt should come up.
        box = driver.find_element("id", "box")
        box.send_keys("Text about to be skipped")
        print("box empty after clearing:", poster.__class__.clear_comment_box(poster, box))
        driver.get(url)
        print("next page loaded with no prompt after clearing the box")

        # And if a box ever is left with text, what happens? Reported, not assumed.
        driver.find_element("id", "box").send_keys("Text left in the box")
        try:
            driver.get(url)
            print("left a filled box: page loaded, no prompt blocked it")
        except Exception as exc:
            print(f"left a filled box: {type(exc).__name__}")
            try:
                driver.switch_to.alert.accept()
                print("left a filled box: accepted the prompt")
            except Exception as inner:
                failures.append(f"could not clear a leave prompt: {inner}")
    except Exception as exc:
        failures.append(f"{type(exc).__name__}: {exc}")
    finally:
        driver.quit()
        shutil.rmtree(workdir, ignore_errors=True)

    if failures:
        print("VERDICT: FAILED", failures)
        return 1
    print("VERDICT: HEALTHY")
    return 0


if __name__ == "__main__":
    sys.exit(main())
