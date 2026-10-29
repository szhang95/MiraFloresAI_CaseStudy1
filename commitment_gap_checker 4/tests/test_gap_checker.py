"""Run from the project folder:  python3 -m unittest discover -s tests -v
No network, no third-party packages. The LLM path is tested with a FAKE reply only."""
import contextlib
import io
import json
import os
import sys
import unittest
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import gap_checker as g  # noqa: E402

TUE = date(2026, 10, 27)  # meeting date used in the samples (a Tuesday)


def sample(name):
    with open(os.path.join(ROOT, "sample_data", name), encoding="utf-8") as fh:
        return fh.read()


def by_sentence(result, fragment):
    matches = [i for i in result["items"] if fragment in i.sentence]
    assert len(matches) == 1, f"expected exactly one item containing {fragment!r}, got {len(matches)}"
    return matches[0]


class DateResolution(unittest.TestCase):
    def check(self, raw, expected):
        d = g.resolve_due(raw, TUE)
        self.assertIsNone(d.problem, d.note)
        self.assertEqual(d.resolved, expected)

    def test_weekday_is_next_occurrence_never_today(self):
        self.check("Thursday", date(2026, 10, 29))
        self.check("Tuesday", date(2026, 11, 3))

    def test_next_weekday_means_following_calendar_week(self):
        self.check("next Friday", date(2026, 11, 6))
        self.check("next Monday", date(2026, 11, 2))

    def test_relative_words(self):
        self.check("tomorrow", date(2026, 10, 28))
        self.check("tonight", TUE)
        self.check("end of the week", date(2026, 10, 30))
        self.check("end of the month", date(2026, 10, 31))
        self.check("in two weeks", date(2026, 11, 10))
        self.check("in 3 business days", date(2026, 10, 30))

    def test_explicit_dates(self):
        self.check("2026-11-03", date(2026, 11, 3))
        self.check("Nov 6", date(2026, 11, 6))
        self.check("6th of November", date(2026, 11, 6))
        self.check("11/06", date(2026, 11, 6))

    def test_yearless_date_far_in_past_rolls_to_next_year(self):
        d = g.resolve_due("Jan 5", date(2026, 12, 20))
        self.assertEqual(d.resolved, date(2027, 1, 5))

    def test_vague_phrases_are_not_dates(self):
        for raw in ["soon", "ASAP", "next week", "at some point", "later"]:
            self.assertEqual(g.resolve_due(raw, TUE).problem, "imprecise", raw)

    def test_impossible_dates_are_rejected(self):
        for raw in ["Feb 30", "2026-02-30", "13/45", "2026-13-01"]:
            self.assertEqual(g.resolve_due(raw, TUE).problem, "invalid", raw)

    def test_deadline_before_meeting_is_flagged(self):
        d = g.resolve_due("Oct 15", TUE)
        self.assertEqual(d.problem, "before_meeting")
        self.assertEqual(g.resolve_due("2026-10-27", TUE).problem, None)  # same day is fine

    def test_precise_beats_vague_in_same_sentence(self):
        kind, raw = g.find_due_phrase("I'll send it by Friday, as soon as possible.")
        self.assertEqual((kind, raw.lower()), ("weekday", "friday"))

    def test_24_7_is_not_a_date(self):
        self.assertEqual(g.find_due_phrase("We offer 24/7 support."), (None, None))


class ExtractionOnSample(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = g.analyse(sample("onboarding_meeting.txt"))

    def test_counts(self):
        s = self.r["summary"]
        self.assertEqual((s["total"], s["complete"], s["needs_attention"]), (14, 4, 10))

    def test_complete_commitment(self):
        i = by_sentence(self.r, "customs document checklist")
        self.assertEqual((i.status, i.owner, i.due_date), ("complete", "Priya", "2026-10-29"))

    def test_named_owner_via_vocative_but_no_date(self):
        i = by_sentence(self.r, "share the SKU list")
        self.assertEqual((i.owner, i.status), ("Tom", "missing_date"))

    def test_hedged_and_vague_date(self):
        i = by_sentence(self.r, "try to get it to you soon")
        self.assertIn("VAGUE_ACTION", i.issues)
        self.assertIn("MISSING_DATE", i.issues)

    def test_group_owner_is_not_an_owner(self):
        i = by_sentence(self.r, "signed data processing agreement")
        self.assertIsNone(i.owner)
        self.assertEqual(i.due_date, "2026-10-30")  # date resolved, owner still missing
        self.assertEqual(i.status, "missing_owner")

    def test_request_to_room_has_no_owner(self):
        i = by_sentence(self.r, "confirm the pickup addresses")
        self.assertIsNone(i.owner)

    def test_unclaimed_large_item_and_silence_note(self):
        i = by_sentence(self.r, "sync shipment status")
        self.assertEqual(i.status, "unclaimed_hard")
        self.assertTrue(any("no one responded" in n for n in i.notes))

    def test_wish_with_hard_keyword_is_unclaimed_hard(self):
        self.assertEqual(by_sentence(self.r, "link each document checkpoint").status, "unclaimed_hard")

    def test_date_before_meeting(self):
        self.assertEqual(by_sentence(self.r, "Oct 15 cutoff").status, "bad_date")

    def test_non_commitments_are_ignored(self):
        sentences = " ".join(i.sentence for i in self.r["items"])
        for text in ["Do you need our SKU list", "main worry", "big piece of work", "Thanks all"]:
            self.assertNotIn(text, sentences)

    def test_lets_without_time_is_not_a_commitment(self):
        self.assertFalse(any("cross-border" in i.sentence for i in self.r["items"]))

    def test_single_participant_on_other_side_is_inferred_and_flagged(self):
        text = ("Meeting date: 2026-10-27\n"
                "Sam (Internal): Can you send the invoices by Friday?\n"
                "Elena (Client): Yes.\n")
        i = g.analyse(text)["items"][0]
        self.assertEqual(i.owner, "Elena")
        self.assertTrue(any("inferred" in n for n in i.notes))

    def test_followup_lists_complete_and_open_items(self):
        d = g.followup_draft(self.r["title"], self.r["meeting"], self.r["items"])
        self.assertIn("Confirmed", d)
        self.assertIn("Still open", d)
        self.assertIn("customs document checklist", d)


class DifficultAndInvalidInputs(unittest.TestCase):
    def test_contradictory_dates_sample(self):
        r = g.analyse(sample("contradictory_dates.txt"))
        self.assertEqual(by_sentence(r, "Feb 30").status, "bad_date")
        self.assertEqual(by_sentence(r, "2026-10-20").status, "bad_date")
        self.assertEqual(by_sentence(r, "13/45").status, "bad_date")
        self.assertEqual(by_sentence(r, "end of the month").status, "complete")

    def test_empty_and_whitespace(self):
        for name in ["empty.txt", "whitespace_only.txt"]:
            with self.assertRaises(g.InputError):
                g.analyse(sample(name))

    def test_garbled(self):
        with self.assertRaises(g.InputError):
            g.analyse(sample("garbled.txt"))

    def test_random_letters_that_are_not_english(self):
        junk = "Meeting date: 2026-10-27\n" + " ".join(["qzxvk wprtl mnbvc xzcvb lkjhg"] * 4)
        with self.assertRaises(g.InputError):
            g.analyse(junk)

    def test_non_english(self):
        with self.assertRaises(g.InputError):
            g.analyse(sample("chinese_transcript.txt"))

    def test_missing_or_bad_meeting_date(self):
        with self.assertRaises(g.InputError):
            g.analyse("Sam (Internal): I'll send it by Friday.")
        with self.assertRaises(g.InputError):
            g.analyse("Sam (Internal): I'll send it by Friday.", date_arg="2026-02-30")

    def test_cli_date_overrides_header(self):
        r = g.analyse("Meeting date: 2026-01-01\nSam (Internal): I'll send it tomorrow.", date_arg="2026-10-27")
        self.assertEqual(r["items"][0].due_date, "2026-10-28")

    def test_valid_input_with_no_commitments(self):
        r = g.analyse(sample("smalltalk.txt"))
        self.assertEqual(r["summary"]["total"], 0)
        self.assertIsNone(r["summary"]["percent_complete"])

    def test_exit_codes(self):
        base = os.path.join(ROOT, "sample_data")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(g.main([os.path.join(base, "smalltalk.txt"), "--no-followup"]), 0)
            self.assertEqual(g.main([os.path.join(base, "empty.txt")]), 2)
            self.assertEqual(g.main([os.path.join(base, "onboarding_meeting.txt"), "--fail-on-gaps", "--no-followup"]), 1)


class LlmReplyHandling(unittest.TestCase):
    """The LLM is never called here. We only test how a reply is checked."""

    TRANSCRIPT = sample("onboarding_meeting.txt")

    def test_verbatim_items_are_kept_and_hallucinated_ones_dropped(self):
        fake = json.dumps([
            {"line": 7, "speaker": "Priya", "sentence": "I'll send the customs document checklist by Thursday.",
             "owner": "Priya", "due": "Thursday", "hedged": False},
            {"line": 99, "speaker": "Sam", "sentence": "I will personally fix everything tomorrow.",
             "owner": "Sam", "due": "tomorrow", "hedged": False},  # never said
        ])
        cands, dropped = g.parse_llm_items(fake, self.TRANSCRIPT)
        self.assertEqual((len(cands), dropped), (1, 1))

    def test_code_fences_are_tolerated(self):
        fake = "```json\n[]\n```"
        self.assertEqual(g.parse_llm_items(fake, self.TRANSCRIPT), ([], 0))

    def test_non_json_reply_is_an_input_error(self):
        with self.assertRaises(g.InputError):
            g.parse_llm_items("Sure! Here are the commitments...", self.TRANSCRIPT)

    def test_llm_date_still_validated_by_code(self):
        fake = json.dumps([{"line": 6, "speaker": "Lena", "sentence": "I'll send the updated address list on Feb 30.",
                            "owner": "Lena", "due": "Feb 30", "hedged": False}])
        cands, _ = g.parse_llm_items(fake, sample("contradictory_dates.txt"))
        items = g.validate(cands, TUE, {"lena": ("Lena", "client")})
        self.assertEqual(items[0].status, "bad_date")


if __name__ == "__main__":
    unittest.main()
