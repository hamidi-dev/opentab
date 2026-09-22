"""Display-only argparse helpers shared by the interactive and JSON CLIs."""

import argparse
import copy
import sys
import textwrap

COMMAND_GROUPS = (
    ("Browse", ("tui", "web")),
    ("Query", ("cost", "usage", "sessions", "models", "sources")),
    ("Conversations", ("conversations",)),
    ("Machines and exports", ("pull", "remote", "export", "forget")),
    ("Saved preferences", ("notes", "bookmarks", "ignore")),
    ("Setup and integration", ("doctor", "mcp")),
)


class _FullHelp(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        parser._print_message(parser.format_help(full=True), file=sys.stdout)
        parser.exit()


class HelpParser(argparse.ArgumentParser):
    """A short task guide by default; the complete reference is always one flag away."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.add_argument(
            "--help-all",
            action=_FullHelp,
            nargs=0,
            default=argparse.SUPPRESS,
            help="show every option, source path and compatibility flag, then exit",
        )
        self.brief_description = None
        self.common_options = None
        self.brief_arguments = {}

    def _get_option_tuples(self, option_string):
        # Adding the full reference must not make existing --he/--hel abbreviations
        # ambiguous. The new flag deliberately requires its exact spelling.
        return [
            item
            for item in super()._get_option_tuples(option_string)
            if not isinstance(item[0], _FullHelp)
        ]

    def format_help(self, *, full=False):
        if full:
            return super().format_help()
        formatter = self._get_formatter()
        formatter.add_usage(self.usage, self._actions, self._mutually_exclusive_groups)
        formatter.add_text(self.brief_description or self.description)

        examples = [line for line in (self.epilog or "").splitlines() if line.startswith("  ")]
        if examples:
            formatter.start_section("Examples")
            formatter.add_text("\n".join(examples))
            formatter.end_section()

        positional, optional = [], []
        for action in self._actions:
            if action.help == argparse.SUPPRESS:
                continue
            if action.option_strings:
                if action.dest not in (self.common_options or ()) and not action.required:
                    continue
                target = optional
            else:
                target = positional
            # Short copy affects only this render, never validation or sibling help.
            display = copy.copy(action)
            display.help = self.brief_arguments.get(action.dest, action.help)
            if display.option_strings:
                display.option_strings = display.option_strings[:1]
                if display.choices and display.metavar is None:
                    display.metavar = {"group_by": "FIELD", "source": "HARNESS"}.get(
                        display.dest, display.dest.upper()
                    )
            target.append(display)
        optional.sort(
            key=lambda action: action.dest in {"pretty", "limit", "offset", "sort", "reverse"}
        )
        for title, actions in (
            (
                "Commands"
                if any(isinstance(a, argparse._SubParsersAction) for a in positional)
                else "Arguments",
                positional,
            ),
            ("Options", optional),
        ):
            if actions:
                formatter.start_section(title)
                formatter.add_arguments(actions)
                formatter.end_section()
        next_steps = []
        if any(isinstance(a, argparse._SubParsersAction) for a in self._actions):
            next_steps.append(f"Next: {self.prog} COMMAND --help")
        if isinstance(formatter, RootHelpFormatter):
            next_steps.append("TUI options: opentab tui --help")
        next_steps.append(f"All options: {self.prog} --help-all")
        formatter.add_text("\n".join(next_steps))
        return formatter.format_help()


class HelpFormatter(argparse.HelpFormatter):
    """Wrap prose while preserving separate, copyable example command lines."""

    def add_usage(self, usage, actions, groups, prefix=None):
        # Only the formatter sees this placeholder. Required permissions, operands
        # and mutually exclusive selectors still use argparse's native rendering.
        mutex_actions = {action for group in groups for action in group._group_actions}
        visible = [
            action
            for action in actions
            if not action.option_strings or action.required or action in mutex_actions
        ]
        for index, action in enumerate(visible):
            if isinstance(action, argparse._SubParsersAction):
                visible[index] = copy.copy(action)
                visible[index].metavar = "COMMAND"
        super().add_usage(usage, [argparse.Action([], dest="[options]"), *visible], groups, prefix)

    def _fill_text(self, text, width, indent):
        return "\n".join(
            indent + line
            if line.startswith("  ")
            else textwrap.fill(
                line,
                width,
                initial_indent=indent,
                subsequent_indent=indent,
                break_long_words=False,
                break_on_hyphens=False,
            )
            for line in text.splitlines()
        )


class RootHelpFormatter(HelpFormatter):
    def add_usage(self, usage, actions, groups, prefix=None):
        argparse.HelpFormatter.add_usage(self, usage, actions, groups, prefix)

    def _format_action(self, action):
        if not isinstance(action, argparse._SubParsersAction):
            return super()._format_action(action)
        choices = {choice.dest: choice for choice in action._choices_actions}
        blocks = []
        for title, names in COMMAND_GROUPS:
            heading = " " * self._current_indent + title + ":\n"
            self._indent()
            entries = "".join(
                super(RootHelpFormatter, self)._format_action(choices[name]) for name in names
            )
            self._dedent()
            blocks.append(heading + entries)
        return "\n".join(blocks)


def arrange_help(parser, *, source_paths=(), legacy=(), common=None, summary=None, brief=None):
    """Regroup existing actions for help only; keep parsing order and ownership intact."""
    parser.formatter_class = HelpFormatter
    parser.brief_description = summary
    parser.brief_arguments = brief or {}
    filters = {
        "range",
        "days",
        "since",
        "until",
        "project",
        "query_harness",
        "machine",
        "model",
        "search",
        "model_search",
        "bookmarked",
        "include_ignored",
        "session",
        "exclude_session",
    }
    output = {"pretty", "limit", "offset", "sort", "reverse", "max_chars"}
    advanced = {"no_state", "no_cache", "no_worktrees", "debug", "debug_log"}
    groups = {
        name: []
        for name in (
            "Options",
            "Filters",
            "Output and pagination",
            "Source selection",
            "Advanced source paths",
            "State, cache and diagnostics",
            "Legacy Options",
        )
    }
    for action in parser._actions:
        if not action.option_strings:
            continue
        dest = action.dest
        title = (
            "Legacy Options"
            if dest in legacy
            else "Advanced source paths"
            if dest in source_paths
            else "Source selection"
            if dest in {"source", "remotes"}
            else "State, cache and diagnostics"
            if dest in advanced
            else "Filters"
            if dest in filters
            else "Output and pagination"
            if dest in output
            else "Options"
        )
        groups[title].append(action)
    parser._optionals.title = "Options"
    parser._optionals._group_actions[:] = groups.pop("Options")
    for title, actions in groups.items():
        if actions:
            group = parser.add_argument_group(title)
            group._group_actions.extend(actions)
    parser.common_options = (
        common
        if common is not None
        else {
            a.dest
            for a in parser._actions
            if a.option_strings
            and a.dest not in {*source_paths, *legacy, *advanced, "version", "help", "help_all"}
        }
    )
