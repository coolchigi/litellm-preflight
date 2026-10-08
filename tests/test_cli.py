"""End-to-end CLI tests, including the README example and the exit codes it documents."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from litellm_preflight import __version__
from litellm_preflight.cli import main

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"


def write_config(path, models=2, **general):
    general = {"background_health_checks": True, **general}
    lines = ["model_list:"]
    lines += [f"  - model_name: m{i}\n    litellm_params: {{model: openai/m{i}}}" for i in range(models)]
    lines.append("general_settings:")
    lines += [f"  {k}: {str(v).lower() if isinstance(v, bool) else v}" for k, v in general.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def run(args, capsys):
    code = main(args)
    out, err = capsys.readouterr()
    return code, out, err


def test_readme_example_matches_real_output(tmp_path, capsys, monkeypatch):
    text = README.read_text(encoding="utf-8")
    command = re.search(r"```bash\n(litellm-preflight health-checks config\.yaml[^\n]*)\n```", text).group(1)
    shown = re.search(r"```text\n(Config: config\.yaml\n.*?)```", text, re.S).group(1)
    monkeypatch.chdir(tmp_path)
    write_config(tmp_path / "config.yaml", models=20)
    code, out, _ = run(command.split()[1:], capsys)
    assert code == 0
    assert out == shown


def test_report_and_cost(tmp_path, capsys):
    code, out, err = run(["health-checks", write_config(tmp_path / "c.yaml"), "--replicas", "3", "--workers", "2",
                          "--cost-per-probe", "0.001"], capsys)
    assert code == 0 and err == ""
    assert "Probes per day: up to 3,456" in out      # 6 processes x 2 deployments x 288 rounds
    assert "You're making 6x that" in out
    assert "$3.46 a day, $103.68 per 30 days" in out


def test_tiny_costs_print_as_decimals(tmp_path, capsys):
    _, out, _ = run(["health-checks", write_config(tmp_path / "c.yaml", models=1), "--cost-per-probe", "0.00001"],
                    capsys)
    assert "Cost at $0.00001 per probe: under $0.01 a day, $0.09 per 30 days" in out


def test_single_process_has_no_multiplier_or_fix(tmp_path, capsys):
    _, out, _ = run(["health-checks", write_config(tmp_path / "c.yaml", models=1)], capsys)
    assert "1 deployment probed" in out
    assert "You're making" not in out and "Fix:" not in out and "Cost" not in out


def test_helm_values_shape_and_override(tmp_path, capsys):
    values = tmp_path / "values.yaml"
    values.write_text("replicaCount: 4\nnumWorkers: 2\nproxy_config:\n  model_list:\n"
                      "    - {model_name: a, litellm_params: {model: openai/a}}\n"
                      "  general_settings: {background_health_checks: true}\n", encoding="utf-8")
    _, out, _ = run(["health-checks", str(values)], capsys)
    assert "Shape from the values file, with any flags you passed applied: 4 replicas x 2 workers" in out
    _, out, _ = run(["health-checks", str(values), "--replicas", "1"], capsys)
    assert "1 replica x 2 workers" in out


def test_bad_config_exits_2_with_message(tmp_path, capsys):
    bad = tmp_path / "c.yaml"
    bad.write_text("- not\n- a mapping\n", encoding="utf-8")
    code, out, err = run(["health-checks", str(bad)], capsys)
    assert code == 2 and out == ""
    assert err.startswith("litellm-preflight: ") and "should be a YAML mapping" in err


def test_missing_file_exits_2(capsys):
    code, _, err = run(["health-checks", "/no/such/config.yaml"], capsys)
    assert code == 2 and "can't read /no/such/config.yaml" in err


def test_backend_flags_need_split(tmp_path, capsys):
    code, _, err = run(["health-checks", write_config(tmp_path / "c.yaml"), "--backend-replicas", "2"], capsys)
    assert code == 2 and "only apply with --split" in err


@pytest.mark.parametrize("flag, value", [
    ("--replicas", "0"), ("--replicas", "-1"), ("--replicas", "abc"), ("--workers", "0"),
    ("--backend-workers", "0"), ("--shared-ttl", "0"), ("--db-deployments", "-1"), ("--restarts-per-day", "-1"),
    ("--cost-per-probe", "-1"), ("--cost-per-probe", "nan"), ("--cost-per-probe", "inf"), ("--cost-per-probe", "x"),
])
def test_bad_flag_values_exit_2(tmp_path, capsys, flag, value):
    with pytest.raises(SystemExit) as exc:
        main(["health-checks", write_config(tmp_path / "c.yaml"), flag, value])
    assert exc.value.code == 2
    assert flag in capsys.readouterr().err


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"litellm-preflight {__version__}"


def test_no_check_exits_2():
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_python_dash_m(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    result = subprocess.run([sys.executable, "-m", "litellm_preflight", "health-checks",
                             write_config(tmp_path / "c.yaml", models=1)], capture_output=True, text=True, env=env)
    assert result.returncode == 0
    assert "Probes per day: up to 288" in result.stdout


def test_include_header(tmp_path, capsys):
    (tmp_path / "models.yaml").write_text("model_list: [{model_name: a, litellm_params: {model: openai/a}}]\n",
                                          encoding="utf-8")
    root = tmp_path / "config.yaml"
    root.write_text("include: [models.yaml]\ngeneral_settings: {background_health_checks: true}\n", encoding="utf-8")
    _, out, _ = run(["health-checks", str(root)], capsys)
    assert "Merged 1 included file: " in out
    assert "1 deployment probed" in out
