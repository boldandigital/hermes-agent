"""The import-time config expansion must not cry wolf about ``~/.hermes/.env`` (Bug 2).

``hermes_cli.config`` expands ``${env:NAME}`` refs at IMPORT time
(``_inject_profile_env_vars`` -> ``providers.list_providers`` -> a config read), which runs BEFORE
``main.py`` calls ``load_hermes_dotenv()``. At that instant ``os.environ`` holds nothing that lives
only in ``~/.hermes/.env``, so every single invocation printed a FALSE
"MINIMAX_API_KEY is not set (check ~/.hermes/.env)" for a key that was present and non-empty.

The warning is still load-bearing: a variable in neither ``os.environ`` nor the dotenv is genuinely
missing and must still be reported.
"""
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# Expand refs in a throwaway home whose .env defines PRESENT and not ABSENT, with NO dotenv
# preload — the import-time ordering the bug lives in.
_EXPAND_PROBE = """
import re
import sys
sys.path.insert(0, sys.argv[2])
from hermes_cli import config
match = re.compile(r'\\${([^}]+)}').match
print(config._env_expand_match(match('${env:PRESENT_IN_DOTENV}')), file=sys.stdout)
print(config._env_expand_match(match('${env:ABSENT_EVERYWHERE}')), file=sys.stdout)
"""


def _expand_refs(home: Path):
    env = {k: v for k, v in __import__('os').environ.items() if k not in ('MINIMAX_API_KEY', 'WP_API_PASSWORD')}
    env.update({'HERMES_HOME': str(home), 'PYTHONPATH': str(ROOT)})
    return subprocess.run([sys.executable, '-c', _EXPAND_PROBE, str(home), str(ROOT)],
                          capture_output=True, text=True, timeout=120, env=env, cwd=str(ROOT), check=False)


@pytest.mark.platforms("posix")
def test_a_var_that_is_in_the_dotenv_does_not_warn_but_a_missing_one_does(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    (home / '.env').write_text('PRESENT_IN_DOTENV=realvalue\n', encoding='utf-8')

    result = _expand_refs(home)
    assert result.returncode == 0, result.stderr

    # The false alarm is gone for the configured key.
    assert "PRESENT_IN_DOTENV is not set" not in result.stderr, result.stderr
    # A key absent from BOTH os.environ and the .env is genuinely missing and still warns.
    assert 'ABSENT_EVERYWHERE is not set (check ~/.hermes/.env)' in result.stderr, result.stderr


@pytest.mark.platforms("posix")
def test_importing_config_emits_no_false_dotenv_warning_for_a_configured_key(tmp_path):
    """The real path: importing hermes_cli.config (what every `hermes` run does) used to warn."""
    home = tmp_path / 'home'
    home.mkdir()
    (home / '.env').write_text('MINIMAX_API_KEY=realvalue\n', encoding='utf-8')
    env = {k: v for k, v in __import__('os').environ.items() if k != 'MINIMAX_API_KEY'}
    env.update({'HERMES_HOME': str(home), 'PYTHONPATH': str(ROOT)})

    result = subprocess.run([sys.executable, '-c', 'import sys; sys.path.insert(0, sys.argv[1]); '
                           'import hermes_cli.config; print("IMPORTED")', str(ROOT)],
                            capture_output=True, text=True, timeout=120, env=env, cwd=str(ROOT), check=False)
    assert result.returncode == 0, result.stderr
    assert 'IMPORTED' in result.stdout
    assert 'is not set (check ~/.hermes/.env)' not in result.stderr, result.stderr