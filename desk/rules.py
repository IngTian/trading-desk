"""Parse rules.md and resolve rules for a scope."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from desk import config


_RUN_ID = re.compile(r"^\s*(?:run)?[\s_-]*(\d+)\s*$", re.IGNORECASE)


def normalize_run_id(run_id: str) -> str:
    """Normalize run ID to run-N format: "2" -> "run-2", "run-2" -> "run-2", "run 2" -> "run-2".

    Refuses anything that does not name exactly one run. Taking the first number anywhere
    made "3b", "run-3-retry" and "r3v2" all run 3, so two runs the owner told apart merged.
    """
    match = _RUN_ID.match(run_id)
    if not match:
        raise ValueError(
            f"{run_id!r} does not name a run: write the run's number, as 3, run-3 or run 3")
    return f"run-{int(match.group(1))}"


# Section names a scope can reach. Runs exist only for paper and replay: --run is refused with
# --account real, so a "Real — run N" section's rules could never apply.
ACCOUNT_SECTIONS = ("All", "Real", "Paper", "Replay")
SPECIAL_SECTIONS = ("Groups", "Changes")
_RUN_SECTION = re.compile(r"^(?:Paper|Replay) — run \d+$")

# A heading line. The section form is exactly two #s and a space; anything else that names a
# section is a typo, caught by _looks_like_a_section.
_HEADING = re.compile(r"^(#{1,6})(\s*)(.*?)\s*$")
# A fence opens with 3+ backticks or tildes. The info string must follow it directly: Task A
# ruled "``` rules" (with a space) a typo to refuse, and that ruling stands.
_FENCE = re.compile(r"^(`{3,}|~{3,})(.*)$")
_BULLET = re.compile(r"^[-*+]\s+(.*)$")


def _is_section_name(name: str) -> bool:
    return name in ACCOUNT_SECTIONS or name in SPECIAL_SECTIONS or bool(_RUN_SECTION.match(name))


def _looks_like_a_section(text: str) -> str | None:
    """The section a mis-formed heading's text names, if it names one, in its proper form."""
    key = re.sub(r"\s+", " ", text.strip()).lower()
    for name in ACCOUNT_SECTIONS + SPECIAL_SECTIONS:
        if key == name.lower():
            return name
    run = re.fullmatch(r"(paper|replay|real)\s*[—–-]+\s*run\s*(\d+)", key)
    if run:
        return f"{run.group(1).capitalize()} — run {int(run.group(2))}"
    return None


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein distance, for telling a mistyped info string from an unrelated one."""
    row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, cb in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (ca != cb))
    return row[-1]


def build_run_section_name(account: str, run_id: str) -> str:
    """Build the section name for a run: "Paper — run 2" for account="paper", run_id="run-2".

    This is the single source of truth for run section names. Both the CLI (when creating runs
    table rows) and for_scope (when looking up sections) must call this function, because their
    correctness is defined entirely by the two agreeing.
    """
    # Normalize the run_id first
    normalized = normalize_run_id(run_id)
    # Build section name: capitalize account, replace hyphens with spaces in run id
    run_title = normalized.replace("-", " ")  # "run-2" -> "run 2"
    return f"{account.capitalize()} — {run_title}"


@dataclass(frozen=True)
class Change:
    """A changelog entry from ## Changes."""
    date: str
    text: str


@dataclass(frozen=True)
class Rules:
    """Resolved rules for one scope (account + run)."""
    numbers: dict[str, str]           # rules with code checks
    ask: tuple[str, ...]              # "Ask me" items, verbatim
    groups: dict[str, tuple[str, ...]]  # ticker groups
    changes: tuple[Change, ...]       # changelog
    hash: str                         # file hash (first 12 chars of sha256)
    path: Path                        # source file
    sections_used: tuple[str, ...]    # composition order (sections found)
    sections_wanted: tuple[str, ...]  # sections looked for (for error messages)
    # Wanted but absent. Not an error -- an All-only scope may be meant -- but never silent:
    # with no "## Replay", a replay trade gets only All's rules, with no risk limit at all.
    sections_missing: tuple[str, ...]
    # Rules with no code check, name -> value. The VALUE is kept, not just the name: the desk
    # reports these as "your rules say `feels_right: strongly` -- no automatic check for this
    # one; I'll ask you each time", and it cannot say that from a name alone.
    unchecked: dict[str, str]


class RulesFile:
    """Parsed rules.md, before scope resolution."""

    def __init__(self, path: Path, content: str):
        self.path = path
        self.content = content
        self.hash = hashlib.sha256(content.encode()).hexdigest()[:12]
        self._sections = self._parse_sections()

    def _refuse(self, line: int, why: str) -> ValueError:
        return ValueError(f"{self.path}:{line}: {why}")

    def _parse_sections(self) -> dict[str, dict]:
        """Parse every `## Section` into its rules, Ask-me questions and change entries.

        One pass, tracking three things: the current section, whether a fence is open (and
        whether it is a rules fence), and whether an Ask-me list is open. Every shape that
        used to drop or mis-scope a rule without a word is now either read the way it reads
        or refused with its line and the fix -- never tolerated:

        - a rule named twice in one section (last-one-wins could loosen it): refused
        - ~~~rules and ````rules fences: read, as Markdown reads them
        - a near-miss info string (```rule, ```Rules, ~~~ rules): refused
        - a heading that names a section but is not `## Name` (##Replay, ### Replay):
          refused, because it was folded into the section above -- into All, for real money
        - a rules fence before the first section: refused, because it belonged to none
        - a wrapped Ask-me question: kept whole, with every question after it
        """
        sections: dict[str, dict] = {}
        section_lines: dict[str, int] = {}
        current: dict | None = None
        fence: tuple[str, int, bool] | None = None   # (char, length, is a rules fence)
        ask_open = False
        ask_after_blank = False

        for number, line in enumerate(self.content.split("\n"), 1):
            stripped = line.strip()

            if fence is not None:
                char, length, is_rules = fence
                if re.fullmatch(re.escape(char) + "{%d,}" % length, stripped):
                    fence = None
                elif is_rules and stripped:
                    self._rule_line(current, number, stripped)
                continue

            opened = _FENCE.match(stripped)
            if opened:
                marks, info = opened.groups()
                is_rules = info == "rules"
                if not is_rules and info and _edit_distance(info.strip().lower(), "rules") <= 2:
                    raise self._refuse(
                        number, f"malformed rules fence {stripped!r} (expected "
                                f"'{marks}rules', with nothing between the fence and 'rules')")
                if is_rules and current is None:
                    raise self._refuse(
                        number, "a rules block before the first '## Section' heading belongs "
                                "to no section, so none of its rules would ever apply")
                fence = (marks[0], len(marks), is_rules)
                ask_open = False
                continue

            heading = _HEADING.match(line)
            if heading:
                hashes, gap, text = heading.groups()
                ask_open = False
                if len(hashes) == 2 and gap:
                    if text in sections:
                        raise self._refuse(
                            number, f"duplicate section {text!r} (already defined at line "
                                    f"{section_lines[text]})")
                    section_lines[text] = number
                    current = sections[text] = {
                        "rules": {}, "rule_lines": {}, "ask": [], "changes": [], "name": text,
                    }
                    continue
                named = _looks_like_a_section(text)
                if named:
                    raise self._refuse(
                        number, f"heading {line.strip()!r} names a section but is not a "
                                f"section heading; write it as '## {named}' (two #s and a "
                                f"space), or its rules are read as part of the section above")
                if current is not None and len(hashes) >= 3 and text.lower() == "ask me":
                    ask_open, ask_after_blank = True, False
                continue

            if current is None:
                continue

            if ask_open:
                ask_open, ask_after_blank = self._ask_line(
                    current["ask"], line, stripped, ask_after_blank)
                continue

            bullet = _BULLET.match(line)
            if current["name"] == "Changes" and bullet:
                current["changes"].append(bullet.group(1).strip())

        if fence is not None:
            raise self._refuse(number, "a fenced block is never closed")
        return sections

    def _rule_line(self, section: dict, number: int, stripped: str) -> None:
        """One `name: value` line inside a rules fence."""
        if ":" not in stripped:
            raise self._refuse(number, f"malformed rule '{stripped}' (expected 'name: value')")
        name, value = (part.strip() for part in stripped.split(":", 1))
        if name in section["rules"]:
            raise self._refuse(
                number, f"{name!r} is set twice in '## {section['name']}' (line "
                        f"{section['rule_lines'][name]} says {section['rules'][name]!r}, this "
                        f"line says {value!r}); keep the one you mean")
        section["rules"][name] = value
        section["rule_lines"][name] = number

    @staticmethod
    def _ask_line(items: list[str], line: str, stripped: str, after_blank: bool) -> tuple[bool, bool]:
        """Feed one line to an open Ask-me list. Returns (still open, last line was blank).

        A question is a `- ` or `* ` bullet. A line that continues it -- indented, or directly
        under it with no blank line between -- is part of it, joined with a space, as Markdown
        renders it. A blank line followed by a bullet is still the list; a blank line followed
        by unindented prose ends it. One wrapped question used to end the whole list.
        """
        bullet = _BULLET.match(line)
        if bullet:
            items.append(bullet.group(1).strip())
            return True, False
        if not stripped:
            return True, True
        if items and (line[0].isspace() or not after_blank):
            items[-1] = f"{items[-1]} {stripped}"
            return True, False
        return False, False

    def unrecognised_sections(self) -> tuple[str, ...]:
        """Sections no scope can ever reach, in file order: their rules never apply.

        Recognised: All, Real, Paper, Replay, Groups, Changes, and "Paper — run N" or
        "Replay — run N". A "Real — run N" is not, since runs are paper or replay only.
        """
        return tuple(name for name in self._sections if not _is_section_name(name))

    def for_scope(
        self, account: str, run_id: str | None, known: frozenset[str]
    ) -> Rules:
        """Resolve rules for a specific scope.

        Composition order: All → account section → run section.
        Later sections override earlier rule names.

        Ask-me items accumulate in scope-composition order (All → account → run),
        the same order rules compose in. Within each section's list, items remain
        in file order relative to each other.

        Args:
            account: "real", "paper", or "replay"
            run_id: Optional run identifier like "run-2" for paper runs (normalized to run-N)
            known: Set of rule names that have code checks (checks.CHECK_NAMES). A name
                outside it lands in `unchecked` WITH its value, so the desk can quote the
                rule back while saying it will ask about it.

        Raises:
            ValueError: When a run_id is provided but no matching run section exists.
        """
        # Normalize run_id if provided
        if run_id:
            run_id = normalize_run_id(run_id)

        # Normalize account name to match section headers
        account_title = account.capitalize()

        # Composition order
        section_names = ["All", account_title]
        if run_id:
            run_section = build_run_section_name(account, run_id)
            section_names.append(run_section)
            # Check that the run section exists
            if run_section not in self._sections:
                available = sorted(self._sections.keys())
                raise ValueError(
                    f"The rules file at {self.path} has no section {run_section!r} for "
                    f"run_id={run_id!r}. Sections found: {available}"
                )

        # Merge rules from sections in order
        merged_rules: dict[str, str] = {}
        merged_ask: list[str] = []
        sections_found: list[str] = []

        for name in section_names:
            if name in self._sections:
                sections_found.append(name)
                section = self._sections[name]
                merged_rules.update(section["rules"])
                merged_ask.extend(section["ask"])

        # Parse Groups section specially
        groups_dict: dict[str, tuple[str, ...]] = {}
        if "Groups" in self._sections:
            for name, value in self._sections["Groups"]["rules"].items():
                # Split comma-separated tickers and strip whitespace
                tickers = tuple(t.strip() for t in value.split(","))
                groups_dict[name] = tickers

        # Parse Changes section specially
        changes_list: list[Change] = []
        if "Changes" in self._sections:
            for item in self._sections["Changes"]["changes"]:
                # Format: "2026-01-15: Description"
                if ":" in item:
                    date, text = item.split(":", 1)
                    changes_list.append(Change(date.strip(), text.strip()))

        # Separate checked vs unchecked rules, both keeping the value as written
        checked: dict[str, str] = {}
        unchecked_rules: dict[str, str] = {}

        for name, value in merged_rules.items():
            if name in known:
                checked[name] = value
            else:
                unchecked_rules[name] = value

        return Rules(
            numbers=checked,
            ask=tuple(merged_ask),
            groups=groups_dict,
            changes=tuple(changes_list),
            hash=self.hash,
            path=self.path,
            sections_used=tuple(sections_found),
            sections_wanted=tuple(section_names),
            sections_missing=tuple(n for n in section_names if n not in sections_found),
            unchecked=unchecked_rules,
        )


def load(path: Path) -> RulesFile:
    """Load and parse a rules.md file."""
    if not path.exists():
        raise FileNotFoundError(
            f"Rules file not found: {path}\n"
            f"Copy the example to get started: cp {config.example_rules()} {path}"
        )
    content = path.read_text()
    return RulesFile(path, content)
