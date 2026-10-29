# Commitment Gap Checker

This tool reads notes from a meeting and tells you which promises still have no owner or no real date. The idea is to catch that gap while everyone is still in the room, not a week later.

I came to this from my last job. In one meeting we talked through several ways to build a customer reminder tool, but nobody wrote anything down or got assigned. Three of us each built the same easy feature that week, and the harder ideas never got touched. A quick check for "who is doing this, and by when?" at the end of that meeting would have caught it.

There are two ways to use it: a web page that runs in your browser, and a command-line version. They use the same rules.

**All the sample meetings are invented.** They are made up for this project, not taken from any real meeting, client or employer.

## Run it in the browser

You need Python 3 (3.9 or newer) and a recent browser (Chrome, Edge, Firefox, or Safari 16.4 or newer). There is nothing to install, no account, and no internet connection needed.

Unzip the project, then open a terminal in the folder that contains `index.html`. If the zip won't open, the same files are on GitHub: https://github.com/szhang95/MiraFloresAI_CaseStudy1 (in that case, open the terminal in the folder that contains `index.html`; it is inside the one subfolder).

```bash
python3 -m http.server 8000
```

Then go to http://localhost:8000. Leave the terminal window open while you use the page.

Don't double-click `index.html`. The examples are loaded with `fetch`, which browsers block for files opened straight from disk. If port 8000 is already taken, use another one, like `python3 -m http.server 8765`.

### What to try

1. Click **Try it with an example meeting**. You should see "10 of 14 promises need a fix" and "4 ready".
2. Read the red box, "Settle these before the meeting ends". It lists six promises nobody owns. The two marked "Big task" come first: "Someone needs to sync shipment status from the carrier API..." and "It would be great to link each document checkpoint to the customer's shipment...".
3. Click the **Need a fix** and **Ready** buttons to filter the list. The ready ones are things like "I'll send the customs document checklist by Thursday" (Priya, due 2026-10-29).
4. Open **Why flagged** under any item to see the reason.
5. In the example menu, choose **Example: impossible dates**. "Feb 30" and "13/45" are flagged as dates that don't exist, and "2026-10-20" is flagged because it is before the meeting (2026-10-27). "The end of the month" is fine and becomes 2026-10-31.
6. Choose **Bad input: empty**, **garbled text** or **not English**. Each shows a red "Can't check this input" box with a short reason.
7. Choose **Example: chat with no promises**. You get "No promises found in these notes."
8. Paste your own notes. Each line should look like `Priya (Internal): I'll send the checklist by Thursday.` Put `Meeting date: 2026-10-27` at the top.
9. Click **Draft follow-up email to the client**, then **Copy message**.

**About the meeting date.** The tool needs to know when the meeting happened so it can turn "Thursday" or "tomorrow" into a real date. It reads that from the `Meeting date:` line in your notes. If your notes don't have that line, open "My notes have no Meeting date line" and type the date there. If you type a date when the notes already have one, yours wins, and the page says so under the button. (Try 2026-11-04 on the example: "Thursday" moves to 2026-11-05 and the invoice promise for 2026-11-03 gets flagged.)

## Command-line version

Same logic, no browser needed. Python 3.9 or newer, no packages.

```bash
python3 gap_checker.py sample_data/onboarding_meeting.txt
python3 gap_checker.py sample_data/contradictory_dates.txt
python3 gap_checker.py sample_data/onboarding_meeting.txt --format json
```

Useful options: `--date YYYY-MM-DD` sets the meeting date, `--no-followup` leaves out the draft email, `--fail-on-gaps` exits with code 1 if anything needs fixing, and `--llm` uses an LLM to find commitments (see below). Exit code 2 means the input couldn't be read.

For the main example it prints:

```
14 commitments found: 4 complete (29%), 10 need attention.
  missing owner: 6   missing/imprecise date: 7   vague wording: 3   date problem: 1   unclaimed large items: 2
```

Bad input gives a one-line message and exit code 2, for example `Input error: the input is empty. Provide a meeting transcript.`

## Tests

```bash
python3 -m unittest discover -s tests -v     # 36 tests, expect "OK"
node tests/parity.js                         # optional, needs Node 18+; expect "10 checks passed"
```

The browser page runs `gap_checker.js`, which is a JavaScript copy of `gap_checker.py`. The Node check runs it on the samples and compares the result with what the Python version produced. That is how I kept the two in step.

## Sample data and expected output

| File in `sample_data/` | What it is | Saved output in `expected_outputs/` |
|---|---|---|
| `onboarding_meeting.txt` | The main example: a client onboarding meeting | `onboarding_meeting.txt`, `onboarding_meeting.json` |
| `contradictory_dates.txt` | The hard case: dates that can't be right | `contradictory_dates.txt` |
| `smalltalk.txt` | Valid notes with no promises in them | `smalltalk.txt` |
| `empty.txt`, `garbled.txt`, `chinese_transcript.txt` | Bad input | `empty.txt`, `garbled.txt`, `chinese_transcript.txt` |

Run the command line on any of these and the output should match the saved file exactly.

## What is real, what is made up, what isn't done

**Built and tested:** finding commitments with rules, turning dates like "Thursday", "next Friday" and "end of the month" into real dates, flagging missing owners and dates, vague wording, impossible dates and large unclaimed tasks, the draft follow-up message, input checks, JSON output, both the browser page and the command line.

**Made up or faked:** all the meeting notes. The follow-up message is only a draft; nothing is ever sent. The optional LLM mode has never been run against the real API. I only tested how it handles replies, using fake ones.

**Not done:** suggesting who should take an unclaimed task, tracking issues across meetings, connecting to shipment or CRM systems, and anything other than English. The browser version has no LLM mode, because calling an API from a web page would expose the key.

## Things that could be wrong

- I only tuned the rules on one invented meeting. I don't know how well they work on real notes, and I expect they'll miss some phrasings and wrongly catch others. One example that came up: "Let's get started" was being counted as a promise. Now "let's" only counts when a date comes with it.
- The "big task" flag is just a list of words (sync, API, integrate, automate and so on). It's a guess I haven't tested.
- Each promise gets one owner at most. "We" doesn't count as an owner. Dates like 11/06 are read as month/day.
- If a date has no year and is more than about six months in the past, I assume it means next year.
- I tried the examples in Chrome and Safari. I haven't tried other browsers.

## The optional LLM mode (command line only)

```bash
export ANTHROPIC_API_KEY=...        # your own key; this can cost money
python3 gap_checker.py sample_data/onboarding_meeting.txt --llm
```

The model only picks out the commitments. Dates are still worked out by ordinary code, and anything the model returns that doesn't appear word for word in the notes is thrown away. If no key is set, it quietly falls back to the rules and says so. Nothing else in this project needs a key.

## How I used AI

I used Claude throughout. It wrote the first versions of the code, the tests and the sample meetings from my descriptions. I chose the problem and the client-meeting scenario, decided what the first version should and shouldn't do, and kept asking for changes when the page was confusing to use. I also used ChatGPT to compare ways of presenting the page and to phrase the write-up. I checked the code by running the tests, reading the output on the sample files, and using the page myself.

I didn't reuse any code from earlier work, and nothing from my previous employer is in here: no code, data or documents. Only the experience behind the problem.

## Time spent

About 3.5 hours: 0.5 framing the problem, 1.75 building, 0.75 testing, 0.5 writing.

## What I'd look at next

1. Run it on 10 to 20 real meeting notes with names removed, and count how often it finds the right promises. Until then I can't say it works.
2. Find out whether a missing owner or a missing date is the more common gap. I think it's the owner, but I haven't measured it.
3. See whether people actually look at the "settle these before the meeting ends" list, or ignore it. That decides whether this is worth building further.
4. Compare the rules with the LLM mode on the same notes.
5. Only if the first three hold up: suggest owners, track repeated issues across meetings, and link promises to shipment and document status.

## Files

```
index.html          the web page
gap_checker.js      the logic, in JavaScript (used by the page)
gap_checker.py      the logic, as a command-line tool
sample_data/        invented meeting notes: normal, hard and bad input
expected_outputs/   saved output for each sample
tests/              Python tests, and the browser-vs-Python check
```
