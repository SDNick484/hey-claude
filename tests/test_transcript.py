import pytest

from hey_claude.transcript import Request, clean_prompt, match_project, normalize, parse, strip_wake

PROJECTS = {"hey claude": "~/src/hey-claude", "dotfiles": "~/.dotfiles", "web": "~/src/web",
            "web api": "~/src/web-api"}


@pytest.mark.parametrize("text,rest", [
    ("Hey Claude, summarize the last five commits.", "summarize the last five commits."),
    ("hey claude what changed today", "what changed today"),
    ("Hi Claude. Open the README", "Open the README"),
    ("Hey, Claude: run the tests", "run the tests"),
    ("Hey Cloud, fix the build", "fix the build"),
    ("Claude, explain this repo", "explain this repo"),
    ("Hey Claude.", ""),
])
def test_strip_wake(text, rest):
    assert strip_wake(text, ["hey claude"]) == (rest, True)


def test_strip_wake_leaves_other_text_alone():
    assert strip_wake("summarize the commits", ["hey claude"]) == ("summarize the commits", False)
    # A name later in the sentence is not a wake phrase.
    assert strip_wake("ask Claude later", ["hey claude"]) == ("ask Claude later", False)


def test_loose_strip_handles_misheard_greeting():
    assert strip_wake("Pay Claude, open the logs", []) == ("Pay Claude, open the logs", False)
    assert strip_wake("Pay Claude, open the logs", [], loose=True) == ("open the logs", True)
    assert strip_wake("Okay so Claude list files", [], loose=True) == ("list files", True)


def test_custom_phrase():
    assert strip_wake("computer, deploy", ["computer"]) == ("deploy", True)


@pytest.mark.parametrize("text,expected", [
    ("", ""), (".", ""), ("Um.", ""), ("yeah", ""), ("--help me", "help me"), ("Fix it.", "Fix it."),
])
def test_clean_prompt(text, expected):
    assert clean_prompt(text) == expected


def test_normalize():
    assert normalize("Hey-Claude, fix_it.") == "hey claude fix it"


@pytest.mark.parametrize("text,project,rest", [
    ("in hey claude, fix the failing build.", "hey claude", "fix the failing build."),
    ("In Hey-Claude fix it", "hey claude", "fix it"),
    ("open dotfiles", "dotfiles", ""),
    ("open dotfiles and update my zshrc", "dotfiles", "update my zshrc"),
    ("for project web: add a login page", "web", "add a login page"),
    ("in web api, add pagination", "web api", "add pagination"),  # longest name wins
    ("in the web, find links", "web", "find links"),
    ("in my notes, find links", None, "in my notes, find links"),
    ("open the readme", None, "open the readme"),
])
def test_match_project(text, project, rest):
    assert match_project(text, PROJECTS) == (project, rest)


def test_parse_end_to_end():
    assert parse("Hey Claude, in dotfiles, tidy my vimrc.", phrases=["hey claude"], projects=PROJECTS) == \
        Request(prompt="tidy my vimrc.", project="dotfiles")
    assert parse("Hey Claude", phrases=["hey claude"], projects=PROJECTS) == Request(prompt="")
