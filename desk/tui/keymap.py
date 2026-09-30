"""Every key and every prompt hint, as data.

The bindings, the footer and the `?` overlay are all generated from these tuples.
That is the point: a hand-written footer can name a key that is not bound, or omit
one that is. Copied from daylogs/tui/keymap.py + hints.py.
"""
from __future__ import annotations

from dataclasses import dataclass

SCOPES = ("app", "dashboard", "positions", "bets")
KINDS = ("nav", "view", "write", "danger")


@dataclass(frozen=True)
class Key:
    key: str
    label: str          # shown in the footer and the help overlay
    action: str         # -> pane.key_<action>(), else app.app_<action>()
    scope: str
    kind: str
    footer: bool = True
    bind: bool = True   # False = delivered by another route (a DataTable message)
    priority: bool = False
    pin: bool = False   # never dropped from a narrow footer


KEYMAP: tuple[Key, ...] = (
    # ── app ──
    # The digits follow TAB ORDER, so 1 is always the leftmost tab. They were bound to
    # panes rather than positions, which meant promoting the dashboard would have left
    # `3` opening tab 1.
    Key("1", "dashboard", "show_dashboard", "app", "nav", footer=False),
    Key("2", "positions", "show_positions", "app", "nav", footer=False),
    # THREE TABS, not four. Capital folded into the dashboard -- the author: "why not just
    # move capital to the dashboard? seems like capital would be a standlone page few
    # info anyway." The digits follow tab order, so `3` moved with it rather than
    # staying on a pane that no longer exists.
    Key("3", "bets", "show_bets", "app", "nav", footer=False),
    Key("left", "prev tab", "prev_tab", "app", "nav", footer=False),
    Key("right", "next tab", "next_tab", "app", "nav", footer=False),
    # REFRESH, not "marks". It pulls closes AND the FX rates the net is translated with,
    # and it is also on a 60-second timer -- the author: "you should have a refresh button that
    # pulls the latest financial data, also, learn from tokscale that we can have this
    # pulling periodic every 60s or so. so that while i open the desk, i can see the
    # balance in real time." The key stays because a timer you cannot pre-empt is a timer
    # you distrust.
    # `m refresh` WAS HERE AND WAS REMOVED on 2026-09-26. The author: "remove the refresh button.
    # we dont need it anymore." It was the only way to get fresh marks while the poll was 60
    # seconds and fired nothing on a write; the desk now fetches on mount, every 30 seconds,
    # and the moment the set of held tickers changes, so pressing it could only ask for
    # something already on its way. The handler goes with it -- a key bound to nothing is how
    # `?` comes to advertise a dead key.
    # The author: "can i switch themes, just like daylogs?" It previews live rather than
    # asking for a name -- a theme name tells you nothing, you have to see it against
    # the arrows and the good/bad colours, which are deliberately NOT themed.
    Key("t", "theme", "theme", "app", "view"),
    Key("u", "undo", "undo", "app", "danger"),
    Key("question_mark", "keys", "help", "app", "view", pin=True),
    Key("escape", "back", "back", "app", "nav", footer=False),
    # Handled inside the prompt, not through dispatch -- but declared here so `?` can
    # document it. A feature nothing mentions is a feature nobody finds: the author had to say
    # "you dont have tab completions it seems" to discover the absence.
    Key("tab", "complete", "complete", "app", "write", footer=False, bind=False),
    Key("q", "quit", "quit", "app", "nav", pin=True),
    # ── positions ──
    Key("n", "fill", "fill", "positions", "write"),
    # `i` IS GONE ENTIRELY. The author: "we should remove that instrument registration entirely
    # from the user ... Dont ever let me use that instrument button. that's not a good UX.
    # The user should never worry about ticker registration. it's completely meaningless
    # and garbage."
    #
    # They are right, and it is worth being precise about why: the registry is a fact the TOOL
    # needs -- a currency to translate with, a symbol to fetch by -- and none of it is a
    # decision about trading. Every field of it now comes from the price source. The one
    # thing only they can answer is WHICH listing, when a ticker has two; that gets a prompt
    # they tab through, not a form they fill in.
    # The dashboard's only BLOCKING obligation was "no stop recorded" and nothing in the
    # tool could write one. A rule you cannot comply with through the tool teaches you to
    # ignore the tool.
    Key("s", "stop", "stop", "positions", "write"),
    # MOVES THE LEG, not a fill. The author moved GOOG to a bet with `enter` -- the only way there
    # was -- and it split the position in two and left a phantom behind, because editing a
    # fill's `!bet` re-adds it under a different (bet, ticker, account) key. A leg's bet is
    # a property of the leg, so it gets its own key and its own UPDATE.
    # LABELLED `leg bet`, NOT `bet`, and the collision is why. `n` on the bets tab already
    # owns the prompt label "bet" for declaring one, and two hints under one label means
    # the wrong grammar renders in the subtitle -- caught here by the test that parses
    # every hint example, which fed `!2026-q4-reverse` to parse_bet and was refused.
    Key("b", "leg bet", "trade_bet", "positions", "write"),
    # NOT BOUND: `enter` arrives as DataTable.RowSelected, and it has to.
    #
    # Binding it here was tried and reverted. DataTable binds `enter` itself, so a
    # focused table wins over an app binding and the dispatch never ran -- and making it
    # `priority` would have taken Enter away from the PROMPT, which is how every line in
    # this tool is submitted. The dashboard, whose focus sits on a scroll container
    # rather than a table, gets it from DashboardPane.on_key instead.
    Key("enter", "edit", "activate", "positions", "nav", bind=False),
    Key("x", "delete", "delete", "positions", "danger"),
    # ── dashboard: the capital ledger, folded in from its own tab ──
    Key("c", "capital", "flow", "dashboard", "write"),
    # THE OFFSET, and it is the reason dividends, fees and interest need no tables of
    # their own. The author: "we will just add a special offset entry into the account balance as
    # i gave you the number to reconcile the difference ... if the diff is small, it's good
    # enough." It sits beside `c` because both are a number and a date that move the
    # account -- what differs is which half of the return they move.
    Key("o", "offset", "offset", "dashboard", "write"),
    Key("x", "delete", "delete", "dashboard", "danger"),
    Key("enter", "edit", "activate", "dashboard", "nav", bind=False),
    # ── dashboard: the one bootstrap ──
    # `b` (record a statement balance) AND `k` (move the as-of clock) BOTH WENT.
    #
    # `b` because the net is derived now -- the author: "why do you bookkeeping a number i did
    # ... from now on, we dont need statements in the tool." A key whose only job was to
    # type in a figure the tool can compute is a key that invites the two to disagree.
    #
    # `k` because the clock existed to decide WHICH RULE VERSION was in force, and the
    # rules are gone. The author asked "what is as-of date" -- and once the answer is "it
    # selects between walls this tool no longer has", there is nothing left to answer.
    #
    # What is left is the one bootstrap: a book with no account can record nothing.
    Key("a", "account", "account", "dashboard", "write"),
    # ── bets ──
    # `n` again, and legitimately: keys are scoped, so `n` means "the new thing
    # this pane makes". lookup() checks the scope before falling back to app.
    Key("n", "bet", "new_bet", "bets", "write"),
    # `e` OPENS $EDITOR, the only key here that leaves the desk. A thesis is paragraphs; the
    # one-line prompt cannot express one and would replace 1,400 characters of reasoning
    # with whatever was typed.
    Key("e", "note", "note", "bets", "write"),
    # SHIFT, because a rename rewrites nine referring columns across the book and is the most
    # invasive thing on this page. `i` would have read better as "id" and is free since the
    # instrument key was deleted -- but a test asserts `i` is bound to nothing, which is a
    # guard against that UX returning, and honouring it costs less than amending it.
    Key("R", "rename", "rename", "bets", "write"),
    # `r` FOR review WAS HERE. It wrote bets.review_date, which no screen ever showed --
    # 0 of 6 bets had one -- and the only thing that read the column was a check.py warning
    # quoting rule 9(h). Dropped 2026-09-27 with the field. The key is free.
    Key("a", "allocation", "allocation", "bets", "write"),
    # The author: "how do i change the bet status?" -- you could not. ai-capex-semis was closed
    # and 2026-q4-reverse made live by hand-written SQL. `s` here mirrors `s` on positions:
    # both set the field that pane's rules are judged on.
    Key("s", "status", "status", "bets", "write"),
)

_TAB_OF = {"dashboard": "tab-dashboard", "positions": "tab-positions",
           "capital": "tab-capital", "bets": "tab-bets"}


def lookup(key: str, scope: str) -> Key | None:
    """Scope first, then app. A pane key shadows nothing — see the invariant test."""
    for k in KEYMAP:
        if k.key == key and k.scope == scope:
            return k
    for k in KEYMAP:
        if k.key == key and k.scope == "app":
            return k
    return None


def app_bindings() -> list[tuple[str, str, str, bool]]:
    """One Binding per bindable key, all routed through dispatch()."""
    priority: dict[str, bool] = {}
    for k in KEYMAP:
        if not k.bind:
            continue
        priority[k.key] = priority.get(k.key, False) or k.priority
    return [(key, f"dispatch('{key}')", "", prio) for key, prio in priority.items()]


def for_scope(scope: str) -> list[Key]:
    return [k for k in KEYMAP if k.scope in (scope, "app") and k.footer]


def help_groups() -> dict[str, list[Key]]:
    out: dict[str, list[Key]] = {}
    for kind in KINDS:
        rows = [k for k in KEYMAP if k.kind == kind]
        if rows:
            out[kind] = rows
    return out


# --------------------------------------------------------------------------- #
# prompt hints — one per label, and a test asserts every label has one
# --------------------------------------------------------------------------- #
# ONE TOKEN FOR THE WHOLE OF `@`. It accepts a date, `today` and `yesterday`;
# spelling all three into every subtitle cost characters the fields that actually
# differ between prompts needed. The `?` overlay carries the expansion.
# (It took a TIME too, until v15 -- see parse.resolve_when for why that went.)
WHEN = "@when"


@dataclass(frozen=True)
class Hint:
    label: str
    example: str
    grammar: str


# EVERY EXAMPLE HERE IS INVENTED, and that is a requirement rather than a style.
# prompt.py assigns hint.example to the Input's PLACEHOLDER, so these render on
# screen and ship inside the binary. They used to be the author's real figures: their TFSA
# total, their cash sleeve, their RRSP deposit and its note, their bet allocation, their INTC
# position. A placeholder is the most-read string in the app.
HINTS: tuple[Hint, ...] = (
    Hint("fill", "ACME 10 12.34 !example-bet @today =4.95",
         f"ticker · ±qty · price · !bet · {WHEN} · =fee · /account · ~note"),
    Hint("capital in", "1000 @2026-01-15 /main ~transfer in",
         f"±amount · CAD or USD · {WHEN} · /account · ~note"),
    Hint("offset", "-4.95 @today ~a fee",
         f"±amount · CAD or USD · {WHEN} · /account · ~note   (cash, not capital)"),
    Hint("account", "fhsa ~First Home Savings",
         "id, typed after / on every fill · ~name · default"),
    Hint("bet status", "closed @today ~why it ended",
         f"status (tab lists them) · {WHEN} it happened · ~why"),
    Hint("stop", "25% =58.40",
         "pct% of the leg · =the price you put at the broker"),
    Hint("leg bet", "Big Techs and Semis",
         "tab cycles the live bets · EMPTY detaches this leg"),
    Hint("rename", "Big Techs and Semis",
         "the name, as words · the id it was created under never changes"),
    Hint("allocation", "5000 @2026-09-08",
         f"amount, frozen · {WHEN} declared · ~why, if re-baselining"),
    # Only ever opened BY the desk, when a ticker matches more than one security.
    Hint("listing", "COIN.TO",
         "tab through the listings · enter picks the one you hold"),
    Hint("bet", "Big Techs and Semis =5000 ~one line if you want one",
         f"name, as words · =allocation · {WHEN} · ~note LAST, it absorbs the rest"),
)


def hint_for(label: str) -> Hint | None:
    return next((h for h in HINTS if h.label == label), None)
