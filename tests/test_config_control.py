import threading
import tomllib

import pytest

from hey_claude import calibrate, config, control, feedback, history


def test_defaults_and_overrides():
    cfg = config.from_dict({"wake": {"threshold": 0.3, "phrases": ["hey claude", "hi claude"]},
                            "launch": {"mode": "tmux"}, "projects": {"Dotfiles": "~/.dotfiles"}})
    assert cfg.wake.threshold == 0.3 and cfg.wake.boost == 1.5
    assert cfg.wake.phrases == ["hey claude", "hi claude"]
    assert cfg.launch.mode == "tmux"
    assert cfg.projects == {"dotfiles": "~/.dotfiles"}


def test_int_accepted_for_float():
    assert config.from_dict({"wake": {"followup_seconds": 5}}).wake.followup_seconds == 5.0


@pytest.mark.parametrize("bad", [{"wake": {"threshold": "high"}}, {"wake": {"enabled": 1}},
                                 {"launch": {"claude_args": "--x"}}, {"projects": {"a": 1}}])
def test_type_errors(bad):
    with pytest.raises(ValueError):
        config.from_dict(bad)


def test_example_config_parses():
    from importlib import resources

    text = resources.files("hey_claude").joinpath("config.example.toml").read_text()
    cfg = config.from_dict(tomllib.loads(text))
    assert cfg == config.Config()  # the example documents the defaults


def test_set_wake_tuning_preserves_other_content(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('# mine\n[wake]\nphrases = ["hey claude"]\nthreshold = 0.25\n\n[launch]\nmode = "tmux"\n')
    config.set_wake_tuning(0.35, 2.0, p)
    data = tomllib.loads(p.read_text())
    assert data["wake"] == {"phrases": ["hey claude"], "threshold": 0.35, "boost": 2.0}
    assert data["launch"] == {"mode": "tmux"}
    assert p.read_text().startswith("# mine\n")


def test_set_wake_tuning_new_file_and_new_section(tmp_path):
    p = tmp_path / "new" / "config.toml"
    config.set_wake_tuning(0.2, 1.0, p)
    assert tomllib.loads(p.read_text()) == {"wake": {"threshold": 0.2, "boost": 1.0}}
    q = tmp_path / "other.toml"
    q.write_text('[launch]\nmode = "tmux"\n')
    config.set_wake_tuning(0.2, 1.0, q)
    assert tomllib.loads(q.read_text()) == {"launch": {"mode": "tmux"}, "wake": {"threshold": 0.2, "boost": 1.0}}


def test_choose_picks_middle_of_widest_range():
    S = calibrate.Score
    scores = [S(0.1, 1.0, 5, 1), S(0.2, 1.0, 5, 0), S(0.3, 1.0, 5, 0), S(0.4, 1.0, 5, 0), S(0.5, 1.0, 2, 0),
              S(0.1, 2.0, 5, 3), S(0.2, 2.0, 5, 0), S(0.3, 2.0, 3, 0)]
    best = calibrate.choose(scores, 5)
    assert (best.threshold, best.boost) == (0.3, 1.0)
    assert calibrate.choose([S(0.1, 1.0, 5, 2)], 5) is None


def test_control_roundtrip(tmp_path):
    hub = control.Hub()
    hub.publish({"state": "idle", "muted": False})
    got = []

    def dispatch(cmd):
        got.append(cmd)
        hub.publish({"state": "idle", "muted": cmd == "mute"})

    path = tmp_path / "run" / "c.sock"
    server = control.Server(hub, dispatch, path)
    server.start()
    try:
        assert control.request("status", path)["status"]["state"] == "idle"
        assert control.request("mute", path)["status"]["muted"] is True
        assert control.request("bogus", path)["ok"] is False
        assert got == ["mute"]

        updates = []
        ready = threading.Event()

        def watcher():
            for st in control.watch(path):
                updates.append(st)
                ready.set()
                if len(updates) == 2:
                    return

        t = threading.Thread(target=watcher, daemon=True)
        t.start()
        assert ready.wait(2)
        hub.publish({"state": "listening", "muted": False})
        t.join(2)
        assert [u["state"] for u in updates] == ["idle", "listening"]

        with pytest.raises(RuntimeError, match="already running"):
            control.Server(hub, dispatch, path).start()
    finally:
        server.stop()
    assert not path.exists()


def test_waybar_format():
    out = feedback.waybar({"state": "listening", "muted": False, "last": "fix it"})
    assert out["class"] == "listening" and "fix it" in out["tooltip"]


def test_history_roundtrip(tmp_path):
    f = tmp_path / "h.jsonl"
    for i in range(3):
        history.record({"prompt": f"p{i}"}, f)
    assert [e["prompt"] for e in history.recent(2, f)] == ["p1", "p2"]
