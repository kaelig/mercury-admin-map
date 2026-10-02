"""U6: registry logic publish uses (KTD16). Pure functions: nothing is written.

Run: cd tools/a11y && python3 -m unittest tests.test_registry -v
"""
import copy
import re
import unittest

from tests import pathshim  # noqa: F401

import registry

ID = re.compile(r"^[a-z0-9.-]+$")
RUN, DATE = "2026-10-02-1400", "2026-10-02"
HAND = ["cards.i-modal-heading-semantics", "settings.i-unnamed-row-buttons"]


class Mint(unittest.TestCase):
    def test_mint_gives_area_and_slug(self):
        reg, iid = registry.mint(registry.empty(), "contrast:color-contrast:aaaaaaaaaaaa", "cards", "contrast",
                                 'text "Muted"', HAND)
        self.assertEqual(iid, "cards.i-a11y-contrast-text-muted")
        self.assertEqual(reg["keys"], {"contrast:color-contrast:aaaaaaaaaaaa": iid})
        self.assertEqual(reg["held"], [iid])
        self.assertEqual(registry.violations(reg, HAND), [])

    def test_existing_key_keeps_its_id(self):
        reg, first = registry.mint(registry.empty(), "k1", "cards", "contrast", 'text "Muted"', HAND)
        reg, again = registry.mint(reg, "k1", "home", "name", 'button "Other"', HAND)
        self.assertEqual(again, first)
        self.assertEqual(reg["held"], [first])

    def test_tombstoned_key_keeps_its_id(self):
        reg, first = registry.mint(registry.empty(), "k1", "cards", "contrast", 'text "Muted"', HAND)
        reg = registry.tombstone(reg, first, "fixed", RUN, DATE)
        reg, again = registry.mint(reg, "k1", "cards", "contrast", 'text "Muted"', HAND)
        self.assertEqual(again, first)

    def test_slug_matching_any_id_ever_held_gets_a_suffix(self):
        reg, first = registry.mint(registry.empty(), "k1", "cards", "contrast", 'text "Muted"', HAND)
        reg = registry.tombstone(reg, first, "withdrawn", RUN, DATE)
        reg, second = registry.mint(reg, "k2", "cards", "contrast", 'text "Muted"', HAND)
        self.assertEqual(second, "cards.i-a11y-contrast-text-muted-2")
        reg, third = registry.mint(reg, "k3", "cards", "contrast", 'text "Muted"', HAND)
        self.assertEqual(third, "cards.i-a11y-contrast-text-muted-3")
        self.assertEqual(reg["held"], sorted([first, second, third]))
        self.assertEqual(registry.violations(reg, HAND), [])

    def test_held_id_is_never_reused_after_its_key_is_gone(self):
        reg = {"keys": {}, "held": ["cards.i-a11y-contrast-text-muted"], "tombstones": {}, "attachments": []}
        _, iid = registry.mint(reg, "k9", "cards", "contrast", 'text "Muted"', HAND)
        self.assertEqual(iid, "cards.i-a11y-contrast-text-muted-2")

    def test_slug_matching_a_hand_found_id_gets_a_suffix(self):
        hand = HAND + ["cards.i-a11y-name-button"]
        _, iid = registry.mint(registry.empty(), "k1", "cards", "name", "button", hand)
        self.assertEqual(iid, "cards.i-a11y-name-button-2")

    def test_every_id_uses_the_ticket_alphabet(self):
        reg = registry.empty()
        for n, element in enumerate(['button "Pay <amount> on <date>"', "Ünïcödé × «r1» ☃", 'link "A.B/C_D"', "",
                                     'text in "API Tokens"', "--__--"]):
            reg, iid = registry.mint(reg, f"k{n}", "settings", "target-size", element, HAND)
            self.assertRegex(iid, ID)
            self.assertTrue(iid.startswith("settings.i-a11y-target-size"), iid)
            self.assertNotIn("--", iid)
            self.assertFalse(iid.endswith("-"), iid)
        self.assertEqual(registry.violations(reg, HAND), [])

    def test_mint_does_not_mutate_its_input(self):
        reg = registry.empty()
        before = copy.deepcopy(reg)
        registry.mint(reg, "k1", "cards", "contrast", 'text "Muted"', HAND)
        self.assertEqual(reg, before)


class Tombstones(unittest.TestCase):
    def setUp(self):
        self.reg, self.iid = registry.mint(registry.empty(), "name:button-name:aaaaaaaaaaaa", "settings", "name",
                                           "button in \"API Tokens\"", HAND)

    def test_tombstone_and_reappear(self):
        reg = registry.tombstone(self.reg, self.iid, "fixed", RUN, DATE)
        self.assertEqual(reg["tombstones"][self.iid],
                         {"reason": "fixed", "attachedTo": None, "run": RUN, "date": DATE})
        self.assertEqual(registry.violations(reg, HAND), [])
        back = registry.untombstone(reg, self.iid)
        self.assertNotIn(self.iid, back["tombstones"])
        self.assertIn(self.iid, back["held"])

    def test_attachment_absorbing_a_published_scanner_id_tombstones_it_as_attached(self):
        key = "name:button-name:aaaaaaaaaaaa"
        reg = registry.attach(self.reg, key, "settings.i-unnamed-row-buttons", RUN, DATE)
        self.assertEqual(reg["tombstones"][self.iid],
                         {"reason": "attached", "attachedTo": "settings.i-unnamed-row-buttons", "run": RUN,
                          "date": DATE})
        self.assertEqual(reg["attachments"],
                         [{"finding": key, "handFound": "settings.i-unnamed-row-buttons", "run": RUN}])
        self.assertEqual(registry.violations(reg, HAND), [])

        released = registry.release(reg, key, "settings.i-unnamed-row-buttons")
        self.assertNotIn(self.iid, released["tombstones"])
        self.assertEqual(released["attachments"], [])
        self.assertEqual(released["keys"][key], self.iid, "a release gives the finding its own id back")
        self.assertEqual(registry.violations(released, HAND), [])

    def test_attaching_a_finding_without_an_id_only_records_the_attachment(self):
        reg = registry.attach(registry.empty(), "name:button-name:bbbbbbbbbbbb", "cards.i-modal-heading-semantics",
                              RUN, DATE)
        self.assertEqual(reg["tombstones"], {})
        self.assertEqual(len(reg["attachments"]), 1)

    def test_many_to_many_release_keeps_the_other_attachment(self):
        key = "name:button-name:aaaaaaaaaaaa"
        reg = registry.attach(self.reg, key, "settings.i-unnamed-row-buttons", RUN, DATE)
        reg = registry.attach(reg, key, "cards.i-modal-heading-semantics", RUN, DATE)
        self.assertEqual(len(reg["attachments"]), 2)
        reg = registry.release(reg, key, "settings.i-unnamed-row-buttons")
        self.assertEqual(reg["tombstones"][self.iid]["attachedTo"], "cards.i-modal-heading-semantics")
        self.assertEqual(registry.violations(reg, HAND), [])

    def test_release_does_not_touch_a_fixed_tombstone(self):
        key = "name:button-name:aaaaaaaaaaaa"
        reg = registry.tombstone(self.reg, self.iid, "fixed", RUN, DATE)
        reg = registry.attach(reg, key, "settings.i-unnamed-row-buttons", RUN, DATE)
        reg = registry.release(reg, key, "settings.i-unnamed-row-buttons")
        self.assertEqual(reg["tombstones"][self.iid]["reason"], "fixed")


class Invariants(unittest.TestCase):
    def test_violations_name_each_broken_invariant(self):
        bad = {
            "keys": {"k1": "cards.i-a11y-x", "k2": "cards.i-a11y-x", "k3": "Cards.I-A11Y_Y",
                     "k4": "settings.i-unnamed-row-buttons"},
            "held": ["cards.i-a11y-x", "Cards.I-A11Y_Y"],
            "tombstones": {"cards.i-a11y-gone": {"reason": "attached", "attachedTo": None, "run": RUN, "date": DATE}},
            "attachments": [],
        }
        found = "\n".join(registry.violations(bad, HAND))
        self.assertIn("cards.i-a11y-x has 2 live keys", found)
        self.assertIn("Cards.I-A11Y_Y", found)
        self.assertIn("settings.i-unnamed-row-buttons", found)
        self.assertIn("cards.i-a11y-gone", found)

    def test_a_tombstoned_id_may_keep_its_key(self):
        reg, iid = registry.mint(registry.empty(), "k1", "cards", "contrast", 'text "Muted"', HAND)
        reg = registry.tombstone(reg, iid, "withdrawn", RUN, DATE)
        self.assertEqual(registry.violations(reg, HAND), [])


if __name__ == "__main__":
    unittest.main()
