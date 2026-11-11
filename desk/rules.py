"""Parse rules.md and resolve rules for a scope."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


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
    sections_used: tuple[str, ...]    # composition order
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

    def _parse_sections(self) -> dict[str, dict]:
        """Parse all sections from the file."""
        sections: dict[str, dict] = {}
        lines = self.content.split("\n")
        i = 0

        while i < len(lines):
            line = lines[i]
            # Match section headers: ## Name
            if line.startswith("## "):
                section_name = line[3:].strip()
                i += 1
                section_data = self._parse_section_content(section_name, lines, i)
                sections[section_name] = section_data
                i = section_data["_end_line"]
            else:
                i += 1

        return sections

    def _parse_section_content(self, section_name: str, lines: list[str], start: int) -> dict:
        """Parse content of a section until next ## header."""
        rules_dict: dict[str, str] = {}
        ask_items: list[str] = []
        change_items: list[str] = []
        end = start

        in_rules_block = False
        in_ask_section = False
        is_changes_section = (section_name == "Changes")

        while end < len(lines):
            line = lines[end]

            # Stop at next section
            if line.startswith("## "):
                break

            # Rules fenced block
            if line.strip() == "```rules":
                in_rules_block = True
                in_ask_section = False
                end += 1
                continue

            if in_rules_block:
                if line.strip() == "```":
                    in_rules_block = False
                    end += 1
                    continue

                # Parse rule line: name: value
                if line.strip():
                    if ":" not in line:
                        raise ValueError(
                            f"{self.path}:{end + 1}: malformed rule '{line.strip()}' "
                            f"(expected 'name: value')"
                        )
                    name, value = line.split(":", 1)
                    rules_dict[name.strip()] = value.strip()
                end += 1
                continue

            # Ask me section
            if line.strip() == "### Ask me":
                in_ask_section = True
                end += 1
                continue

            if in_ask_section:
                if line.startswith("- "):
                    ask_items.append(line[2:])  # Keep verbatim after "- "
                elif line.strip() and not line.startswith("#"):
                    # Non-empty, non-header line ends ask section
                    in_ask_section = False
                end += 1
                continue

            # List items in Changes section
            if is_changes_section and line.startswith("- "):
                change_items.append(line[2:])
                end += 1
                continue

            end += 1

        return {
            "rules": rules_dict,
            "ask": ask_items,
            "changes": change_items,
            "_end_line": end,
        }

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
            run_id: Optional run identifier like "run-2" for paper runs
            known: Set of rule names that have code checks (checks.CHECK_NAMES). A name
                outside it lands in `unchecked` WITH its value, so the desk can quote the
                rule back while saying it will ask about it.
        """
        # Normalize account name to match section headers
        account_title = account.capitalize()

        # Composition order
        section_names = ["All", account_title]
        if run_id:
            # Format: "Paper — run 2" for run_id="run-2"
            run_title = run_id.replace("-", " ")
            section_names.append(f"{account_title} — {run_title}")

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
            unchecked=unchecked_rules,
        )


def load(path: Path) -> RulesFile:
    """Load and parse a rules.md file."""
    if not path.exists():
        raise FileNotFoundError(f"Rules file not found: {path}")
    content = path.read_text()
    return RulesFile(path, content)
