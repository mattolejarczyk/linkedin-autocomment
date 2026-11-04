# Review run: you click Comment, the tool never does

A way to work through drafted comments by hand. Each post opens in the profile's
logged-in Chrome with its draft already typed into LinkedIn's comment box. You read
it, edit in the box if you want, and click Comment yourself. The next post then loads.

## Turn on the guard

In `data/<profile>/profile_config.json`:

```json
"review": { "human_click_only": true }
```

With this on, every path that could submit a comment refuses: the poster, the
dashboard's Post button, the scheduler's posting job and `first_comment`. An
unreadable config counts as on.

Drafts are not protected without it. The dashboard's Post button and the scheduler
post from every GENERATED draft, approved or not.

## Run it

```
python -m linkedin_automation.review_run --profile <name>
```

| Option | What it does |
|---|---|
| `--count N` | Review at most N posts |
| `--approved-only` | Only posts already approved on the review screen |
| `--no-window` | Skip and Stop from the console instead of the small window |
| `--allow-production` | Needed for a declared production identity |
| `--mark-commented URL` | Record a comment you posted by hand. No browser |
| `--release KEY` | Put a held draft back in the queue. No browser |

To change a comment, edit it right in LinkedIn's box and click Comment there. You do
not need the window for that.

The small always-on-top window shows where you are, with Skip and Stop. Skip asks
why: pick a reason from the list, or choose Other and type your own. A skip with no
reason is refused. "Not now" stays in the queue; every other reason moves the draft
to the Trash view, where it can be restored.

**I posted it** moves on when the run has not noticed your click. It records that you
said so and marks the post as commented, so it is not offered again. It clicks
nothing. It always logs `posted_on_their_word` and the run ends DEGRADED, because the
run needed telling. `final_text` is filled only if your text could be read back.

**Stop** ends the run. If the current post has no decision yet, you may have clicked
Comment, so it is logged `unknown`, its page is saved, and the draft is held until you
settle it with `--mark-commented` or `--release`.

The panel also has a box for your own words, kept exactly as you type them. Mark them
as a direction ("this is too negative, find a point of agreement first") or as your
own version of the comment. **Use this** puts your own version in LinkedIn's box and
does not skip. You still read it and click Comment. A direction, or a version you
skip with, is only logged.

The browser closes at the end. The run ends with HEALTHY, DEGRADED or FAILED, in its output and
exit code (0, 2, 1).

## How it sees your click

It reads the page about once a second and changes nothing. A comment counts as
posted when your text appears in a posted comment, even if the box still reads as
full, which happens on some pages. It also counts when the box is empty or gone and
the last text seen in the box is in the thread, or anywhere on the page if no comment
selector matches. The log's `box_emptied` says whether the box emptied. A comment changed
after the run last looked at the box still counts, but `final_text` is then the last
text it saw, so read `page_excerpt` beside it: that is what the page showed. Each post's page is
saved the moment its box first empties, and `data/<profile>/review_trace/` records
what the run saw as it waited. If the box empties and the thread changes but the text cannot be found, the
outcome is `unknown`, never a guess. An unknown draft is held back and not offered
again until you settle it with `--mark-commented` or `--release`, because it may
already be on LinkedIn.

## What gets logged

`data/<profile>/review_log.jsonl`, one line per post: `draft_original`,
`approved_text`, `loaded_text`, `final_text`, `skip_reason`, `skip_note`,
`feedback_type` (`direction` or `my_version`), `feedback_text`, `feedback_reason`,
`feedback_used_in_box`, and an `outcome` of `posted_as_is`, `posted_on_their_word`, `posted_edited`, `skipped`, `unknown`, `failed`,
`unavailable` or `already_commented`. Failures name their evidence file.

The log never changes a persona or voice setting.

## Morning scan

```
python -m linkedin_automation.morning_scan --profile <name>
```

Tops the review queue up to `review.daily_limit`. If the queue is already full it
does not open a browser. It never posts. `--status-only` refreshes the status file
with no browser. To schedule it, run `tools/register_morning_scan.ps1` yourself.

`data/<profile>/review_status.json` holds:

| Field | Meaning |
|---|---|
| `verdict` | `HEALTHY`, `DEGRADED` or `FAILED` for the last scan |
| `reasons` | Why, for example `login_required`, `browser_in_use`, `queue_full`, `no_scan_yet` |
| `ready` | Drafts ready now, always counted from the store, even after a failed scan |
| `oldest_ready_generated_at` | When the oldest ready draft was written |
| `scan_started_at`, `scan_finished_at`, `scan_skipped` | The last scan's timing |
| `posts_seen`, `qualified`, `new_drafts` | What it found |
| `evidence` | The log file to read when something went wrong |
| `status_written_at` | When this file was last written |

## Length limits

Set `max_words` and `max_sentences` in the profile's `comment_generator` block to make a
length a rule. A draft over either is retried and never queued, and each rejection is
saved in `reply_failures.jsonl`. `comment_length_range` is only a request to the model.
Neither applies to text you edit yourself.

## Settings

The `review` block of the profile config, with defaults:

| Key | Default | Meaning |
|---|---|---|
| `human_click_only` | `false` | Only the human submits comments |
| `daily_limit` | `5` | Most drafts in the queue and in one run |
| `max_draft_age_days` | `3` | Older drafts stop counting as ready |
| `scan_max_posts` | `30` | Most posts one scan reads |
| `poll_seconds` | `0.75` | How often the run looks at the page |
| `posted_confirm_seconds` | `20` | How long to look for the text before saying `unknown` |
| `min_seconds_between_posts` | `20` | Shortest gap between loading posts |

## Checking Chrome can wait

```
python tools/review_wait_test.py --minutes 20
```

Uses a throwaway Chrome profile and a local page. Never opens LinkedIn.
