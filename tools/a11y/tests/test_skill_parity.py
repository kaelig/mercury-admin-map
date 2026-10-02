"""The a11y-audit skill matches the tooling it drives (U11, KTD26).

- Every `audit.py <command> [sub]` that SKILL.md names is a stage `audit.py --help` lists.
- Each prompt in references/ starts with its version id, and every example line in it carries that id.
- .claude/skills/a11y-audit is a relative symlink to the skill directory.
- The drop reason codes in verifier.md are exactly the ones verdict.py accepts.
"""
import json
import os
import re
import subprocess
import sys
import unittest

from tests.pathshim import A11Y, ROOT

import verdict

SKILL_DIR = os.path.join(ROOT, ".agents", "skills", "a11y-audit")
SKILL = os.path.join(SKILL_DIR, "SKILL.md")
LINK = os.path.join(ROOT, ".claude", "skills", "a11y-audit")
PROMPTS = {"recorder": "recorder.md", "record-judge": "record-judge.md", "verifier": "verifier.md"}

MENTION = re.compile(r"audit\.py\s+([a-z][a-z-]*)(?:[ \t]+([a-z][a-z-]*))?")
HELP_ROW = re.compile(r"^  (\S+(?: \S+)?)\s{2,}\S")
CODE_ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|")
FENCE = re.compile(r"```[a-z]*\n(.*?)```", re.S)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def help_stages():
    """{command: {subs}} from the commands section of `audit.py --help`."""
    out = subprocess.run([sys.executable, os.path.join(A11Y, "audit.py"), "--help"],
                         capture_output=True, text=True, check=True).stdout
    stages = {}
    for line in out.split("commands:", 1)[1].splitlines():
        m = HELP_ROW.match(line)
        if m:
            cmd, _, sub = m.group(1).partition(" ")
            stages.setdefault(cmd, set())
            if sub:
                stages[cmd].add(sub)
    return stages


def example_lines(text):
    """Every JSON object line inside a fenced block of a prompt file."""
    lines = []
    for block in FENCE.findall(text):
        for raw in block.splitlines():
            raw = raw.strip()
            if raw.startswith("{") and raw.endswith("}"):
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    lines.append(obj)
    return lines


def section(text, heading):
    """The lines under a `## heading`, up to the next `## `."""
    out, inside = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            inside = line[3:].strip().lower() == heading.lower()
            continue
        if inside:
            out.append(line)
    return out


class StageCommands(unittest.TestCase):
    def test_help_lists_stages(self):
        stages = help_stages()
        self.assertIn("status", stages)
        self.assertEqual(stages["verdict"], {"add"})

    def test_every_stage_skill_md_names_is_listed_by_help(self):
        stages = help_stages()
        mentions = MENTION.findall(read(SKILL))
        self.assertGreaterEqual(len(mentions), 10, "SKILL.md should name its stages as `audit.py <command> [sub]`")
        unknown = []
        for cmd, word in mentions:
            if cmd not in stages:
                unknown.append(f"audit.py {cmd}")
            elif stages[cmd] and word not in stages[cmd]:
                unknown.append(f"audit.py {cmd} {word}".strip())
        self.assertEqual(sorted(set(unknown)), [], "stages SKILL.md names that `audit.py --help` does not list")

    def test_skill_md_starts_with_status_after_an_interruption(self):
        body = read(SKILL).split("---", 2)[2]
        first = "\n".join(body.strip().splitlines()[:8])
        self.assertIn("audit.py status", first)


class PromptVersions(unittest.TestCase):
    def test_each_prompt_starts_with_its_version_id(self):
        for name, file in PROMPTS.items():
            with self.subTest(prompt=file):
                first = read(os.path.join(SKILL_DIR, "references", file)).splitlines()[0].strip()
                self.assertRegex(first, rf"^{re.escape(name)}@\d+$")

    def test_every_example_line_carries_the_version_id(self):
        for name, file in PROMPTS.items():
            with self.subTest(prompt=file):
                text = read(os.path.join(SKILL_DIR, "references", file))
                version = text.splitlines()[0].strip()
                carried = [x for x in example_lines(text) if "prompt" in x]
                self.assertTrue(carried, f"{file} has no example line with a prompt field")
                self.assertEqual({x["prompt"] for x in carried}, {version})


class SkillLink(unittest.TestCase):
    def test_claude_skill_is_a_relative_symlink_to_the_skill_directory(self):
        self.assertTrue(os.path.islink(LINK), f"{LINK} is not a symlink")
        self.assertEqual(os.readlink(LINK), "../../.agents/skills/a11y-audit")
        self.assertEqual(os.path.realpath(LINK), os.path.realpath(SKILL_DIR))

    def test_the_skill_loads_through_the_link(self):
        text = read(os.path.join(LINK, "SKILL.md"))
        self.assertTrue(text.startswith("---\n"))
        front = text.split("---", 2)[1]
        self.assertRegex(front, r"(?m)^name: a11y-audit$")
        self.assertRegex(front, r"(?m)^description: \S")


class ReasonCodes(unittest.TestCase):
    def setUp(self):
        self.text = read(os.path.join(SKILL_DIR, "references", "verifier.md"))

    def test_the_drop_code_table_matches_verdict_py(self):
        codes = [m.group(1) for line in section(self.text, "Drop reason codes") if (m := CODE_ROW.match(line))]
        codes = [c for c in codes if c != "code"]
        self.assertEqual(len(codes), len(set(codes)), "a code is listed twice")
        self.assertEqual(sorted(codes), sorted(verdict.DROP_CODES))

    def test_no_other_exception_code_appears(self):
        found = set(re.findall(r"exception:[a-z-]+", self.text))
        self.assertEqual(found - set(verdict.DROP_CODES), set())

    def test_example_verdicts_use_accepted_codes_and_outcomes(self):
        verdicts = [x for x in example_lines(self.text) if "outcome" in x]
        self.assertTrue(verdicts)
        for v in verdicts:
            self.assertIn(v["outcome"], verdict.OUTCOMES)
            self.assertIn(v.get("reasonCode"), set(verdict.DROP_CODES) | {None})


if __name__ == "__main__":
    unittest.main()
