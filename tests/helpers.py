import contextlib
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'src'))
FIX = os.path.join(HERE, 'fixtures')
CLAUDE_DIR = os.path.join(FIX, 'claude', 'projects')
CODEX_DIR = os.path.join(FIX, 'codex')
ANTHROPIC_PRICES = os.path.join(FIX, 'prices', 'anthropic.toml')
OPENAI_PRICES = os.path.join(FIX, 'prices', 'openai.toml')

from anatomy import cli  # noqa: E402


def run_cli(*extra):
    """Run `anatomy scan` in-process on the fixtures. -> (exit code, stdout, stderr)."""
    argv = ['scan', '--claude-dir', CLAUDE_DIR, '--codex-dir', CODEX_DIR, '--workers', '1',
            '--anthropic-prices', ANTHROPIC_PRICES, '--openai-prices', OPENAI_PRICES, *extra]
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def scan_json(*extra):
    code, out, err = run_cli('--json', *extra)
    assert code == 0, err
    return json.loads(out)
