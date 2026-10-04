"""Shared-element signatures (KTD15).

Run: cd tools/a11y && python3 -m unittest tests.test_signature -v
"""
import re
import unittest

from tests import pathshim  # noqa: F401

import signature as sig

KEY = re.compile(r"^[a-z-]+:[a-z-]+:[0-9a-f]{12}$")


def occ(tag="button", role="button", name="", classes=(), ancestors=(), landmark=None, heading="", data=None,
        xpath="/html/body/div[2]/main/button[1]", rect=(10, 10, 24, 24)):
    return {"tag": tag, "role": role, "name": name, "classes": list(classes),
            "ancestors": [list(a) for a in ancestors], "landmark": landmark or {"role": "main", "name": ""},
            "heading": heading, "data": data or {}, "xpath": xpath, "rect": list(rect), "layer": "document"}


ICON_BUTTON = ["_button_btn01_5", "_icon_btn01_9", "fa", "svg-inline--fa"]
ROW_ACTIONS = [["_rowActions_tbl22_14"], ["_row_tbl22_3"], ["_page_pg777_1"]]
HEADER = [["_header_hdr33_2"], ["_page_pg777_1"]]


class ClassNormalization(unittest.TestCase):
    def test_module_classes_become_local_names_and_fontawesome_is_dropped(self):
        self.assertEqual(
            sig.local_classes(["_root_ab12c_3", "_ghost_ab12c_9", "fa", "fa-solid", "fa-chevron-down",
                               "svg-inline--fa", "sr-only", "_root_zz99y_1"]),
            ["ghost", "root", "sr-only"])

    def test_module_hashes(self):
        self.assertEqual(sig.module_hashes(["_root_ab12c_3", "_x_zz99y_1", "plain"]), {"ab12c", "zz99y"})


class IntrinsicKeys(unittest.TestCase):
    def test_key_is_readable(self):
        k = sig.finding_key("contrast", "color-contrast", "intrinsic",
                            occ(tag="span", role="", classes=["_muted_ab12c_3"]),
                            {"fgColor": "#9d9da8", "bgColor": "#ffffff"})
        self.assertRegex(k, KEY)
        self.assertTrue(k.startswith("contrast:color-contrast:"))

    def test_new_build_hashes_keep_the_key(self):
        a = occ(classes=["_chip_ab12c_3", "_active_ab12c_9"], ancestors=[["_list_qq111_2"]])
        b = occ(classes=["_chip_x9y8z_30", "_active_x9y8z_41"], ancestors=[["_list_mm222_7"]])
        self.assertEqual(sig.finding_key("target-size", "target-size", "intrinsic", a),
                         sig.finding_key("target-size", "target-size", "intrinsic", b))

    def test_local_name_role_and_tag_change_the_key(self):
        base = sig.finding_key("target-size", "target-size", "intrinsic", occ(classes=["_chip_ab12c_3"]))
        for other in (occ(classes=["_pill_ab12c_3"]), occ(role="link", classes=["_chip_ab12c_3"]),
                      occ(tag="a", classes=["_chip_ab12c_3"])):
            self.assertNotEqual(base, sig.finding_key("target-size", "target-size", "intrinsic", other))

    def test_intrinsic_key_ignores_name_and_call_site(self):
        a = occ(name="Delete", classes=ICON_BUTTON, ancestors=ROW_ACTIONS)
        b = occ(name="Close", classes=ICON_BUTTON, ancestors=HEADER)
        self.assertEqual(sig.finding_key("target-size", "target-size-own", "intrinsic", a),
                         sig.finding_key("target-size", "target-size-own", "intrinsic", b))

    def test_contrast_adds_rounded_colors(self):
        o = occ(tag="span", role="", classes=["_muted_ab12c_3"])

        def key(fg, bg):
            return sig.finding_key("contrast", "color-contrast", "intrinsic", o, {"fgColor": fg, "bgColor": bg})

        base = key("#9d9da8", "#ffffff")
        self.assertEqual(base, key("#9e9da8", "#fefefe"), "one step of rendering noise stays one finding")
        self.assertEqual(base, key("rgb(157, 157, 168)", "#FFF"), "notation does not matter")
        self.assertNotEqual(base, key("#6b6b75", "#ffffff"))
        self.assertNotEqual(base, key("#9d9da8", "#f0f0f3"))

    def test_rounded_color(self):
        self.assertEqual(sig.parse_color("#9D9DA8"), (157, 157, 168))
        self.assertEqual(sig.parse_color("#fff"), (255, 255, 255))
        self.assertEqual(sig.parse_color("rgba(1, 2, 3, 0.5)"), (1, 2, 3))
        self.assertIsNone(sig.parse_color(None))
        self.assertIsNone(sig.parse_color("transparent"))
        self.assertEqual(sig.round_color("#9d9da8"), sig.round_color("#9e9da8"))


class ContentKeys(unittest.TestCase):
    def test_new_build_hashes_keep_the_key(self):
        a = occ(classes=["_button_btn01_5"], ancestors=[["_wrap_btn01_2"], ["_rowActions_tbl22_14"]])
        b = occ(classes=["_button_kk555_6"], ancestors=[["_wrap_kk555_3"], ["_rowActions_pp666_15"]])
        self.assertEqual(sig.finding_key("name", "button-name", "content", a),
                         sig.finding_key("name", "button-name", "content", b))

    def test_react_ids_keep_the_key(self):
        a = occ(tag="input", role="textbox", name="Search :r18:", data={"data-for": ":r18:"}, heading="Find «r18»")
        b = occ(tag="input", role="textbox", name="Search :r16:", data={"data-for": ":r16:"}, heading="Find «r16»")
        self.assertEqual(sig.finding_key("name", "label", "content", a),
                         sig.finding_key("name", "label", "content", b))

    def test_nth_child_positions_keep_the_key(self):
        a = occ(tag="li", role="", data={"data-target": "li:nth-child(3) > button"}, xpath="/html/body/ul/li[3]")
        b = occ(tag="li", role="", data={"data-target": "li:nth-child(7) > button"}, xpath="/html/body/ul/li[7]")
        self.assertEqual(sig.finding_key("name", "label", "content", a),
                         sig.finding_key("name", "label", "content", b))

    def test_amounts_and_dates_share_one_template_and_key(self):
        self.assertEqual(sig.name_template("Pay $1,203.00 on Oct 2"), "Pay <amount> on <date>")
        self.assertEqual(sig.name_template("Pay $90.00 on Sep 1"), "Pay <amount> on <date>")
        a = occ(name="Pay $1,203.00 on Oct 2", classes=["_primary_btn01_7"], ancestors=[["_footer_pay44_2"]])
        b = occ(name="Pay $90.00 on Sep 1", classes=["_primary_btn01_7"], ancestors=[["_footer_pay44_2"]])
        self.assertEqual(sig.finding_key("name", "label-content-name-mismatch", "content", a),
                         sig.finding_key("name", "label-content-name-mismatch", "content", b))
        c = occ(name="Cancel", classes=["_primary_btn01_7"], ancestors=[["_footer_pay44_2"]])
        self.assertNotEqual(sig.finding_key("name", "label-content-name-mismatch", "content", a),
                            sig.finding_key("name", "label-content-name-mismatch", "content", c))

    def test_two_call_sites_give_two_content_keys(self):
        row = occ(classes=ICON_BUTTON, ancestors=ROW_ACTIONS)
        head = occ(classes=ICON_BUTTON, ancestors=HEADER)
        self.assertNotEqual(sig.finding_key("name", "button-name", "content", row),
                            sig.finding_key("name", "button-name", "content", head))

    def test_call_site_is_the_nearest_ancestor_from_another_module(self):
        # The first ancestor belongs to the button's own module (btn01), so it is skipped.
        a = occ(classes=["_button_btn01_5"], ancestors=[["_inner_btn01_2"], ["_toolbar_tlb88_1"], ["_page_pg777_1"]])
        same_module_changed = occ(classes=["_button_btn01_5"],
                                  ancestors=[["_innerWide_btn01_3"], ["_toolbar_tlb88_1"], ["_page_pg777_1"]])
        far_changed = occ(classes=["_button_btn01_5"],
                          ancestors=[["_inner_btn01_2"], ["_toolbar_tlb88_1"], ["_settings_set99_1"]])
        call_site_changed = occ(classes=["_button_btn01_5"],
                                ancestors=[["_inner_btn01_2"], ["_sidebar_sid77_1"], ["_page_pg777_1"]])
        k = sig.finding_key("name", "button-name", "content", a)
        self.assertEqual(k, sig.finding_key("name", "button-name", "content", same_module_changed))
        self.assertEqual(k, sig.finding_key("name", "button-name", "content", far_changed))
        self.assertNotEqual(k, sig.finding_key("name", "button-name", "content", call_site_changed))

    def test_no_module_classes_fall_back_to_landmark_and_data_hooks(self):
        base = occ(classes=[], ancestors=[["_toolbar_tlb88_1"]], landmark={"role": "main", "name": ""},
                   data={"data-testid": "save"})
        moved = occ(classes=[], ancestors=[["_other_oth11_1"]], landmark={"role": "main", "name": ""},
                    data={"data-testid": "save"})
        nav = occ(classes=[], ancestors=[["_toolbar_tlb88_1"]], landmark={"role": "navigation", "name": ""},
                  data={"data-testid": "save"})
        hook = occ(classes=[], ancestors=[["_toolbar_tlb88_1"]], landmark={"role": "main", "name": ""},
                   data={"data-testid": "cancel"})
        k = sig.finding_key("name", "button-name", "content", base)
        self.assertEqual(k, sig.finding_key("name", "button-name", "content", moved))
        self.assertNotEqual(k, sig.finding_key("name", "button-name", "content", nav))
        self.assertNotEqual(k, sig.finding_key("name", "button-name", "content", hook))

    def test_family_and_rule_are_part_of_the_key(self):
        o = occ(classes=ICON_BUTTON, ancestors=ROW_ACTIONS)
        self.assertNotEqual(sig.finding_key("name", "button-name", "content", o),
                            sig.finding_key("name", "aria-command-name", "content", o))
        self.assertNotEqual(sig.finding_key("name", "button-name", "content", o),
                            sig.finding_key("role-state", "button-name", "content", o))


class IncompleteKeys(unittest.TestCase):
    """KTD12: axe's incomplete results group by rule and messageKey, product-wide, not per element."""

    def popup_triggers(self, n=48):
        return [occ(tag=("button", "a", "div")[i % 3], role=("button", "link", "combobox")[i % 3], name=f"Options {i}",
                    classes=[f"_trigger{i}_mn{i:03d}_1"], ancestors=[[f"_toolbar{i}_tb{i:03d}_2"]],
                    xpath=f"/html/body/div[{i + 1}]/button") for i in range(n)]

    def test_48_elements_share_one_key_per_rule_and_message_key(self):
        triggers = self.popup_triggers()
        per_element = {sig.finding_key("role-state", "aria-valid-attr-value", "content", o) for o in triggers}
        self.assertEqual(len(per_element), 48, "the elements really are 48 different shared elements")
        k = sig.incomplete_key("role-state", "aria-valid-attr-value", "controlsWithinPopup")
        self.assertRegex(k, r"^role-state:aria-valid-attr-value:[0-9a-f]{12}$")
        self.assertEqual(k, "role-state:aria-valid-attr-value:" + sig.sha(
            {"family": "role-state", "rule": "aria-valid-attr-value", "basis": "incomplete",
             "messageKey": "controlsWithinPopup"})[:12])
        self.assertNotIn(k, per_element)

    def test_message_keys_split_and_null_is_its_own_group(self):
        keys = {sig.incomplete_key("contrast", "color-contrast", m) for m in ("bgOverlap", "bgImage", None)}
        self.assertEqual(len(keys), 3)
        self.assertEqual(sig.incomplete_key("contrast", "color-contrast", None),
                         sig.incomplete_key("contrast", "color-contrast", None))

    def test_family_and_rule_are_part_of_the_key(self):
        k = sig.incomplete_key("role-state", "aria-valid-attr-value", "noId")
        self.assertNotEqual(k, sig.incomplete_key("name", "aria-valid-attr-value", "noId"))
        self.assertNotEqual(k, sig.incomplete_key("role-state", "aria-prohibited-attr", "noId"))


class RecordSignature(unittest.TestCase):
    def record(self, template, o):
        return {"role": o["role"], "name": o["name"], "states": {}, "description": "", "template": template,
                "occurrence": o}

    def test_record_sig_survives_new_hashes_and_splits_on_template(self):
        a = self.record('button "<n> more"', occ(name="3 more", classes=["_more_ab12c_1"], ancestors=[["_card_cc111_2"]]))
        b = self.record('button "<n> more"', occ(name="12 more", classes=["_more_zz999_4"], ancestors=[["_card_dd222_5"]]))
        c = self.record('button "<n> more" [expanded=false]',
                        occ(name="3 more", classes=["_more_ab12c_1"], ancestors=[["_card_cc111_2"]]))
        self.assertRegex(sig.record_sig(a), r"^rec:[0-9a-f]{12}$")
        self.assertEqual(sig.record_sig(a), sig.record_sig(b))
        self.assertNotEqual(sig.record_sig(a), sig.record_sig(c))

    def test_record_finding_key(self):
        k = sig.record_finding_key("name", "rec:0123456789ab")
        self.assertRegex(k, r"^name:announced-as:[0-9a-f]{12}$")
        self.assertNotEqual(k, sig.record_finding_key("role-state", "rec:0123456789ab"))

    def test_hand_found_key(self):
        self.assertEqual(sig.hand_key("settings.i-unnamed-row-buttons"), "hand:settings.i-unnamed-row-buttons")


class GroupShape(unittest.TestCase):
    def test_agreement_counts_the_most_common_tag_and_attribute_set(self):
        buttons = [occ(tag="button") for _ in range(9)]
        self.assertEqual(sig.agreement(buttons + [occ(tag="a")]), 0.9)
        self.assertEqual(sig.agreement(buttons[:8] + [occ(tag="a"), occ(tag="div")]), 0.8)
        self.assertEqual(sig.agreement([occ(data={"data-x": "a"}), occ(data={"data-x": "b"})]), 0.5)
        self.assertEqual(sig.agreement([occ(classes=["_chip_aaaaa_1"]), occ(classes=["_chip_bbbbb_1"])]), 0.5)
        self.assertEqual(sig.agreement([occ(name="One", xpath="/a"), occ(name="Two", xpath="/b")]), 1.0)

    def test_agreement_ignores_interaction_state_attributes(self):
        focused = occ(data={"data-focus-visible": "true", "data-active-item": "true"})
        self.assertEqual(sig.agreement([focused, occ(), occ(data={"data-discover": "true"})]), 1.0)
        self.assertEqual(sig.agreement([occ(data={"data-testid": "a", "data-focus-visible": "true"}),
                                        occ(data={"data-testid": "b"})]), 0.5)

    def test_class_distance(self):
        a = occ(classes=["_a_aaaaa_1"], ancestors=[["_b_bbbbb_1"]])
        self.assertEqual(sig.class_distance(a, a), 0)
        self.assertEqual(sig.class_distance(a, occ(classes=["_a_zzzzz_9"], ancestors=[["_b_yyyyy_2"]])), 0)
        self.assertEqual(sig.class_distance(a, occ(classes=["_a_aaaaa_1"], ancestors=[["_c_ccccc_1"]])), 0.6667)
        self.assertEqual(sig.class_distance(a, occ(classes=["_x_aaaaa_1"], ancestors=[["_y_ccccc_1"]])), 1)


if __name__ == "__main__":
    unittest.main()
