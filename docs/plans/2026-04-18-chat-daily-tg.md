# chat-daily-tg Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a macOS local Python tool that runs daily at 08:00, exports WeChat group chats from the previous day, deduplicates + summarizes via LLM (through CLIProxyAPI), pushes a concise summary to the user's Telegram bot, and maintains a long-term opportunities database.

**Architecture:** Single-machine pipeline orchestrated by `run_daily.py`. Wraps the existing `wx-cli` binary (reads local WeChat data) and calls a local CLIProxyAPI HTTP endpoint for LLM inference (uses OAuth Claude Code subscription, no paid API key). Outputs layered: concise Telegram push + detailed local markdown archive + structured JSONL opportunity DB + rolling hot-leads board. Scheduled by launchd.

**Tech Stack:** Python 3.11+, `httpx` (HTTP client), `pyyaml` (config), `pydantic` (data validation), `tenacity` (retry), `pytest` (tests), launchd (scheduling), `wx-cli` (WeChat data), CLIProxyAPI (LLM proxy).

**Spec reference:** `/Users/Apple/projects/chat-daily-tg/docs/specs/2026-04-18-design.md`

---

## Project File Structure

```
/Users/Apple/projects/chat-daily-tg/
├── run_daily.py                        # Entry point (orchestrator)
├── src/chat_daily_tg/
│   ├── __init__.py                     # Package marker
│   ├── paths.py                        # Path constants (data dir, log dir, ...)
│   ├── config.py                       # Load & validate config.yaml
│   ├── wx_exporter.py                  # Subprocess wrapper around `wx` CLI
│   ├── archive.py                      # Write daily raw md files to archive/
│   ├── llm_client.py                   # CLIProxyAPI HTTP client
│   ├── prompts.py                      # LLM prompt templates
│   ├── summarizer.py                   # Call LLM, parse response
│   ├── tg_sender.py                    # Telegram Bot API client
│   ├── notifier.py                     # macOS notification via osascript
│   ├── logging_setup.py                # Structured logging config
│   ├── fingerprint.py                  # URL/invite-code/MD5/phone extractors
│   ├── dedup.py                        # Cross-group content deduplication
│   ├── db.py                           # permanent.jsonl read/write
│   ├── permanent_md.py                 # Regenerate permanent.md from jsonl
│   ├── hot_leads.py                    # Hot-leads board + 14-day roll-off
│   └── death_signals.py                # Parse & apply death signals
├── tests/
│   ├── conftest.py                     # Shared fixtures (temp dirs, fake data)
│   ├── fixtures/
│   │   └── sample_chat.md              # Sample wx-cli export for tests
│   ├── test_config.py
│   ├── test_wx_exporter.py
│   ├── test_archive.py
│   ├── test_llm_client.py
│   ├── test_tg_sender.py
│   ├── test_notifier.py
│   ├── test_fingerprint.py
│   ├── test_dedup.py
│   ├── test_db.py
│   ├── test_permanent_md.py
│   ├── test_hot_leads.py
│   └── test_death_signals.py
├── launchd/
│   └── com.apple.chat-daily-tg.plist
├── docs/
│   ├── specs/2026-04-18-design.md
│   └── plans/2026-04-18-chat-daily-tg.md  # this file
├── pyproject.toml
├── README.md
└── .gitignore
```

**Data directory (not in git):** `~/chat-daily/` — config.yaml, permanent.jsonl, permanent.md, hot-leads/, archive/, logs/

---

## Phase 0 — Prerequisites

Assumes: wx-cli already installed, codesign completed, `sudo wx init` run, 3 groups exist in `wx sessions`, CLIProxyAPI running on `127.0.0.1:8317` with `claude-sonnet-4-6` reachable, Telegram bot `@Taoli98Bot` created.

### Task 0.0: Project scaffold + git init

**Files:**
- Create: `/Users/Apple/projects/chat-daily-tg/.gitignore`
- Create: `/Users/Apple/projects/chat-daily-tg/pyproject.toml`
- Create: `/Users/Apple/projects/chat-daily-tg/README.md`
- Create: `/Users/Apple/projects/chat-daily-tg/src/chat_daily_tg/__init__.py`
- Create: `/Users/Apple/projects/chat-daily-tg/tests/conftest.py` (empty)

- [ ] **Step 1: Initialize git repo and create Python project skeleton**

```bash
cd /Users/Apple/projects/chat-daily-tg
git init
python3 -m venv venv
source venv/bin/activate
mkdir -p src/chat_daily_tg tests/fixtures launchd
touch src/chat_daily_tg/__init__.py tests/__init__.py tests/conftest.py
```

- [ ] **Step 2: Write `.gitignore`**

Create `/Users/Apple/projects/chat-daily-tg/.gitignore`:

```gitignore
# Python
__pycache__/
*.py[cod]
*.egg-info/
.pytest_cache/
venv/
.venv/

# IDE
.vscode/
.idea/
.cursor/

# macOS
.DS_Store

# Local secrets / runtime
.env
*.log

# Never commit real data
/data/
/output/
```

- [ ] **Step 3: Write `pyproject.toml`**

Create `/Users/Apple/projects/chat-daily-tg/pyproject.toml`:

```toml
[project]
name = "chat-daily-tg"
version = "0.1.0"
description = "Daily WeChat group summary to Telegram via local LLM proxy"
requires-python = ">=3.11"
dependencies = [
    "httpx>=0.27",
    "pyyaml>=6.0",
    "pydantic>=2.5",
    "tenacity>=8.2",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-httpx>=0.30",
    "pytest-mock>=3.12",
]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
python_files = "test_*.py"
addopts = "-v --tb=short"
```

- [ ] **Step 4: Install deps**

```bash
cd /Users/Apple/projects/chat-daily-tg
source venv/bin/activate
pip install -e ".[dev]"
```

Expected: installs httpx, pyyaml, pydantic, tenacity, pytest without error.

- [ ] **Step 5: Minimal README**

Create `/Users/Apple/projects/chat-daily-tg/README.md`:

```markdown
# chat-daily-tg

Daily WeChat group summary to Telegram via local CLIProxyAPI.

See `docs/specs/2026-04-18-design.md` for design.
See `docs/plans/2026-04-18-chat-daily-tg.md` for implementation plan.

## Run

```bash
source venv/bin/activate
python run_daily.py
```

## Test

```bash
pytest
```
```

- [ ] **Step 6: First commit**

```bash
cd /Users/Apple/projects/chat-daily-tg
git add .gitignore pyproject.toml README.md src/ tests/ docs/
git commit -m "chore: scaffold project with pyproject.toml and git"
```

Expected: commit succeeds with ~10 files staged.

---

### Task 0.1: Verify CLIProxyAPI reachable and model works

**Files:** none (manual validation)

- [ ] **Step 1: curl /v1/models to confirm claude-sonnet-4-6 is listed**

```bash
curl -s http://127.0.0.1:8317/v1/models \
  -H "Authorization: Bearer $CLIPROXY_API_KEY" | \
  python3 -c "import json,sys; data=json.load(sys.stdin); ids=[m['id'] for m in data['data']]; print('\n'.join(ids))" | \
  grep -F claude-sonnet-4-6
```

Expected: prints `claude-sonnet-4-6` on a line.

- [ ] **Step 2: curl /v1/chat/completions with a tiny prompt**

```bash
curl -s http://127.0.0.1:8317/v1/chat/completions \
  -H "Authorization: Bearer $CLIPROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4-6","messages":[{"role":"user","content":"ping"}],"max_tokens":20}' | \
  python3 -c "import json,sys; d=json.load(sys.stdin); print(d['choices'][0]['message']['content'])"
```

Expected: prints a short reply (e.g., "Pong!" or similar).

- [ ] **Step 3: Ensure `CLIPROXY_API_KEY` is set in `~/.zshenv`**

```bash
grep CLIPROXY_API_KEY ~/.zshenv || echo "MISSING"
```

If missing, add:

```bash
echo 'export CLIPROXY_API_KEY="4a4afe4a18abf0d03305d5989a8acb00927a2a6fd18d3410"' >> ~/.zshenv
```

(Replace with the actual key from `~/CLIProxyAPI/config.yaml` `api-keys[0]`.)

Reload shell: `source ~/.zshenv`.

---

### Task 0.2: Provision Telegram bot chat_id

**Files:** none (manual, result goes into env var).

- [ ] **Step 1: Send `/start` to @Taoli98Bot from your Telegram account**

Open Telegram, find `@Taoli98Bot`, tap "Start" (or send `/start`).

- [ ] **Step 2: Fetch chat_id via getUpdates**

You'll need the bot token. Get it from @BotFather → `/mybots` → select `@Taoli98Bot` → "API Token".

```bash
export TG_BOT_TOKEN="<paste-bot-token>"
curl -s "https://api.telegram.org/bot${TG_BOT_TOKEN}/getUpdates" | \
  python3 -c "import json,sys; d=json.load(sys.stdin); print(d['result'][-1]['message']['chat']['id'] if d.get('result') else 'no updates — send /start to bot first')"
```

Expected: prints a numeric chat_id like `123456789`.

- [ ] **Step 3: Persist both values to `~/.zshenv`**

```bash
echo 'export TG_BOT_TOKEN="<token>"' >> ~/.zshenv
echo 'export TG_CHAT_ID="<chat-id>"' >> ~/.zshenv
source ~/.zshenv
```

- [ ] **Step 4: Test-send a message**

```bash
curl -s -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
  -d "chat_id=${TG_CHAT_ID}" \
  -d "text=chat-daily-tg 配置检查通过"
```

Expected: your Telegram app pings and shows the test message.

---

### Task 0.3: Confirm 3 target WeChat groups exist

**Files:** none.

- [ ] **Step 1: List recent sessions and grep for each group**

```bash
wx sessions --limit 100 --json | \
  python3 -c "import json,sys; data=json.load(sys.stdin); names=[s['chat'] for s in data if s.get('is_group')]; target=['贝利知识星球VIP群❤️','贝利知识星球VIP2️⃣群❤️','OpenCLI 交流群']; [print(f\"{'✓' if t in names else '✗'} {t}\") for t in target]"
```

Expected: all three print with `✓`. If any shows `✗`, note the correct exact name (run `wx sessions --json | grep -i opencli` etc.) and record — you'll use it in Task 1.1 config.

- [ ] **Step 2: Test a one-day export**

```bash
mkdir -p /tmp/wx-verify && \
  wx export '贝利知识星球VIP群❤️' --since 2026-04-17 --until 2026-04-18 \
  --format markdown -o /tmp/wx-verify/test.md && \
  wc -l /tmp/wx-verify/test.md
```

Expected: non-zero line count, confirms wx-cli still works.

---

## Phase 1 — MVP pipeline (manual trigger only, no automation yet)

### Task 1.1: Config loader with Pydantic validation

**Files:**
- Create: `src/chat_daily_tg/paths.py`
- Create: `src/chat_daily_tg/config.py`
- Create: `tests/test_config.py`
- Create: `~/chat-daily/config.yaml` (sample)

- [ ] **Step 1: Write path constants module**

Create `src/chat_daily_tg/paths.py`:

```python
from __future__ import annotations
from pathlib import Path

DATA_DIR = Path.home() / "chat-daily"
CONFIG_PATH = DATA_DIR / "config.yaml"
PERMANENT_JSONL = DATA_DIR / "permanent.jsonl"
PERMANENT_MD = DATA_DIR / "permanent.md"
HOT_LEADS_DIR = DATA_DIR / "hot-leads"
HOT_LEADS_LATEST = HOT_LEADS_DIR / "latest.md"
ARCHIVE_DIR = DATA_DIR / "archive"
LOG_DIR = DATA_DIR / "logs"


def archive_dir_for(date_str: str) -> Path:
    """`date_str` is YYYY-MM-DD → returns archive/YYYY/MM/DD path."""
    y, m, d = date_str.split("-")
    return ARCHIVE_DIR / y / m / d


def hot_leads_day_file(date_str: str) -> Path:
    """`date_str` is YYYY-MM-DD → returns hot-leads/YYYY/MM/DD.md path."""
    y, m, d = date_str.split("-")
    return HOT_LEADS_DIR / y / m / f"{d}.md"


def log_file_for(date_str: str) -> Path:
    return LOG_DIR / f"{date_str}.log"
```

- [ ] **Step 2: Write failing test for config loader**

Create `tests/test_config.py`:

```python
from pathlib import Path
import pytest
from chat_daily_tg.config import Config, load_config


def test_load_config_reads_yaml(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
groups:
  - "Group A"
  - "Group B"
schedule:
  time: "08:00"
  coverage: "yesterday"
  timezone: "Asia/Shanghai"
hot_leads:
  retention_days: 14
llm:
  endpoint: "http://127.0.0.1:8317/v1"
  model: "claude-sonnet-4-6"
  api_key_env: "CLIPROXY_API_KEY"
  max_tokens: 8000
telegram:
  bot_token_env: "TG_BOT_TOKEN"
  chat_id_env: "TG_CHAT_ID"
retry:
  max_attempts: 3
  backoff_seconds: [5, 15, 60]
sanitize:
  enabled: false
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    assert cfg.groups == ["Group A", "Group B"]
    assert cfg.llm.model == "claude-sonnet-4-6"
    assert cfg.llm.endpoint == "http://127.0.0.1:8317/v1"
    assert cfg.hot_leads.retention_days == 14
    assert cfg.schedule.timezone == "Asia/Shanghai"
    assert cfg.sanitize.enabled is False


def test_load_config_missing_groups_raises(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("groups: []\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(cfg_file)
```

- [ ] **Step 3: Run tests, verify they fail**

```bash
cd /Users/Apple/projects/chat-daily-tg
source venv/bin/activate
pytest tests/test_config.py -v
```

Expected: FAIL — `ModuleNotFoundError: chat_daily_tg.config` or similar.

- [ ] **Step 4: Implement config module**

Create `src/chat_daily_tg/config.py`:

```python
from __future__ import annotations
from pathlib import Path
from typing import Literal
import yaml
from pydantic import BaseModel, Field, field_validator


class Schedule(BaseModel):
    time: str = "08:00"
    coverage: Literal["yesterday"] = "yesterday"
    timezone: str = "Asia/Shanghai"


class HotLeads(BaseModel):
    retention_days: int = 14


class LLM(BaseModel):
    endpoint: str
    model: str
    api_key_env: str
    max_tokens: int = 8000


class Telegram(BaseModel):
    bot_token_env: str
    chat_id_env: str


class Retry(BaseModel):
    max_attempts: int = 3
    backoff_seconds: list[int] = Field(default_factory=lambda: [5, 15, 60])


class Sanitize(BaseModel):
    enabled: bool = False


class Config(BaseModel):
    groups: list[str]
    todo: list[str] = Field(default_factory=list)
    schedule: Schedule = Field(default_factory=Schedule)
    hot_leads: HotLeads = Field(default_factory=HotLeads)
    llm: LLM
    telegram: Telegram
    retry: Retry = Field(default_factory=Retry)
    sanitize: Sanitize = Field(default_factory=Sanitize)

    @field_validator("groups")
    @classmethod
    def groups_nonempty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("groups must contain at least one group name")
        return v


def load_config(path: Path) -> Config:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return Config(**raw)
```

- [ ] **Step 5: Run tests, verify they pass**

```bash
pytest tests/test_config.py -v
```

Expected: 2 PASSED.

- [ ] **Step 6: Create the real user config file**

```bash
mkdir -p ~/chat-daily
cat > ~/chat-daily/config.yaml <<'YAML'
groups:
  - "贝利知识星球VIP群❤️"
  - "贝利知识星球VIP2️⃣群❤️"
  - "OpenCLI 交流群"

todo: []

schedule:
  time: "08:00"
  coverage: "yesterday"
  timezone: "Asia/Shanghai"

hot_leads:
  retention_days: 14

llm:
  endpoint: "http://127.0.0.1:8317/v1"
  model: "claude-sonnet-4-6"
  api_key_env: "CLIPROXY_API_KEY"
  max_tokens: 8000

telegram:
  bot_token_env: "TG_BOT_TOKEN"
  chat_id_env: "TG_CHAT_ID"

retry:
  max_attempts: 3
  backoff_seconds: [5, 15, 60]

sanitize:
  enabled: false
YAML
```

If any group name came back different in Task 0.3, substitute that exact name here.

- [ ] **Step 7: Commit**

```bash
git add src/chat_daily_tg/paths.py src/chat_daily_tg/config.py tests/test_config.py
git commit -m "feat(config): pydantic-validated YAML config loader"
```

---

### Task 1.2: wx-cli export wrapper

**Files:**
- Create: `src/chat_daily_tg/wx_exporter.py`
- Create: `tests/test_wx_exporter.py`

- [ ] **Step 1: Write failing test using subprocess mock**

Create `tests/test_wx_exporter.py`:

```python
from pathlib import Path
from unittest.mock import patch, MagicMock
from chat_daily_tg.wx_exporter import export_group


def test_export_group_builds_correct_command(tmp_path: Path):
    out_path = tmp_path / "out.md"
    with patch("chat_daily_tg.wx_exporter.subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout="已导出 42 条消息", stderr="")
        result = export_group(
            group_name="贝利VIP",
            since="2026-04-17",
            until="2026-04-18",
            out_path=out_path,
        )
    # confirm subprocess called with correct args
    called_args = run.call_args[0][0]
    assert called_args[0].endswith("wx")
    assert "export" in called_args
    assert "贝利VIP" in called_args
    assert "--since" in called_args
    assert "2026-04-17" in called_args
    assert "--until" in called_args
    assert "2026-04-18" in called_args
    assert "--format" in called_args
    assert "markdown" in called_args
    assert "-o" in called_args
    assert str(out_path) in called_args
    assert result.message_count == 42


def test_export_group_nonzero_exit_raises(tmp_path: Path):
    out_path = tmp_path / "out.md"
    with patch("chat_daily_tg.wx_exporter.subprocess.run") as run:
        run.return_value = MagicMock(returncode=1, stdout="", stderr="group not found")
        import pytest
        with pytest.raises(RuntimeError, match="group not found"):
            export_group("missing", "2026-04-17", "2026-04-18", out_path)
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
pytest tests/test_wx_exporter.py -v
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement wx_exporter module**

Create `src/chat_daily_tg/wx_exporter.py`:

```python
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import subprocess


WX_BINARY = shutil.which("wx") or "/opt/homebrew/bin/wx"


@dataclass(frozen=True)
class ExportResult:
    group_name: str
    out_path: Path
    message_count: int


def export_group(
    group_name: str,
    since: str,
    until: str,
    out_path: Path,
    limit: int = 10000,
) -> ExportResult:
    """Run `wx export <group>` for a single day.

    Raises RuntimeError on non-zero exit.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        WX_BINARY,
        "export",
        group_name,
        "--since", since,
        "--until", until,
        "--limit", str(limit),
        "--format", "markdown",
        "-o", str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"wx export failed: {proc.stderr or proc.stdout}")
    # Parse "已导出 N 条消息" from stdout
    m = re.search(r"已导出\s+(\d+)\s+条消息", proc.stdout)
    count = int(m.group(1)) if m else 0
    return ExportResult(group_name=group_name, out_path=out_path, message_count=count)
```

- [ ] **Step 4: Run tests, verify they pass**

```bash
pytest tests/test_wx_exporter.py -v
```

Expected: 2 PASSED.

- [ ] **Step 5: Commit**

```bash
git add src/chat_daily_tg/wx_exporter.py tests/test_wx_exporter.py
git commit -m "feat(wx_exporter): subprocess wrapper around wx export"
```

---

### Task 1.3: Archive writer (place exports under archive/YYYY/MM/DD/)

**Files:**
- Create: `src/chat_daily_tg/archive.py`
- Create: `tests/test_archive.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_archive.py`:

```python
from pathlib import Path
from chat_daily_tg.archive import safe_filename, prepare_archive_day


def test_safe_filename_replaces_slashes_and_colons():
    assert safe_filename("foo/bar:baz") == "foo_bar_baz"


def test_safe_filename_keeps_chinese_and_emoji():
    assert safe_filename("贝利VIP❤️") == "贝利VIP❤️"


def test_prepare_archive_day_creates_nested_dirs(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("chat_daily_tg.archive.ARCHIVE_DIR", tmp_path / "archive")
    p = prepare_archive_day("2026-04-17")
    assert p == tmp_path / "archive" / "2026" / "04" / "17"
    assert p.is_dir()
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
pytest tests/test_archive.py -v
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement archive module**

Create `src/chat_daily_tg/archive.py`:

```python
from __future__ import annotations
from pathlib import Path
from chat_daily_tg.paths import ARCHIVE_DIR, archive_dir_for


def safe_filename(name: str) -> str:
    """Sanitize group name for use as filename. Keep unicode; strip only unsafe chars."""
    unsafe = "/:\\\x00"
    out = name
    for c in unsafe:
        out = out.replace(c, "_")
    return out


def prepare_archive_day(date_str: str) -> Path:
    """Create archive/YYYY/MM/DD/ and return path."""
    d = archive_dir_for(date_str)
    d.mkdir(parents=True, exist_ok=True)
    return d
```

- [ ] **Step 4: Run tests, verify they pass**

```bash
pytest tests/test_archive.py -v
```

Expected: 3 PASSED.

- [ ] **Step 5: Commit**

```bash
git add src/chat_daily_tg/archive.py tests/test_archive.py
git commit -m "feat(archive): safe filename + archive day directory helper"
```

---

### Task 1.4: LLM client (CLIProxyAPI via OpenAI-compat)

**Files:**
- Create: `src/chat_daily_tg/llm_client.py`
- Create: `tests/test_llm_client.py`

- [ ] **Step 1: Write failing test using httpx mock**

Create `tests/test_llm_client.py`:

```python
import httpx
import pytest
from pytest_httpx import HTTPXMock
from chat_daily_tg.llm_client import LLMClient


def test_chat_completion_posts_correct_shape(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="http://127.0.0.1:8317/v1/chat/completions",
        method="POST",
        json={
            "choices": [{"message": {"role": "assistant", "content": "hello"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
    )
    client = LLMClient(
        endpoint="http://127.0.0.1:8317/v1",
        model="claude-sonnet-4-6",
        api_key="test-key",
        max_tokens=100,
    )
    text, usage = client.chat("say hi")
    assert text == "hello"
    assert usage["total_tokens"] == 15

    sent = httpx_mock.get_request()
    assert sent.headers["Authorization"] == "Bearer test-key"
    body = sent.read().decode()
    assert '"model":"claude-sonnet-4-6"' in body.replace(" ", "")
    assert '"max_tokens":100' in body.replace(" ", "")
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
pytest tests/test_llm_client.py -v
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement LLM client**

Create `src/chat_daily_tg/llm_client.py`:

```python
from __future__ import annotations
from dataclasses import dataclass
import httpx


@dataclass
class LLMClient:
    endpoint: str
    model: str
    api_key: str
    max_tokens: int = 8000
    timeout: float = 120.0

    def chat(self, prompt: str, system: str | None = None) -> tuple[str, dict]:
        """Single-turn completion. Returns (content, usage_dict)."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(
                f"{self.endpoint}/chat/completions",
                json=payload,
                headers=headers,
            )
            r.raise_for_status()
            data = r.json()
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        return content, usage
```

- [ ] **Step 4: Run tests, verify they pass**

```bash
pytest tests/test_llm_client.py -v
```

Expected: 1 PASSED.

- [ ] **Step 5: Smoke-test against real CLIProxyAPI**

```bash
cd /Users/Apple/projects/chat-daily-tg
source venv/bin/activate
python -c "
import os
from chat_daily_tg.llm_client import LLMClient
c = LLMClient(
    endpoint='http://127.0.0.1:8317/v1',
    model='claude-sonnet-4-6',
    api_key=os.environ['CLIPROXY_API_KEY'],
    max_tokens=50,
)
text, usage = c.chat('用一句话确认你收到消息')
print('content:', text)
print('usage:', usage)
"
```

Expected: prints a short Chinese reply and token usage with non-zero total_tokens.

- [ ] **Step 6: Commit**

```bash
git add src/chat_daily_tg/llm_client.py tests/test_llm_client.py
git commit -m "feat(llm_client): CLIProxyAPI OpenAI-compat client"
```

---

### Task 1.5: Telegram sender (with 4096-char message splitting)

**Files:**
- Create: `src/chat_daily_tg/tg_sender.py`
- Create: `tests/test_tg_sender.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_tg_sender.py`:

```python
from pytest_httpx import HTTPXMock
import pytest
from chat_daily_tg.tg_sender import TelegramSender, split_message


def test_split_message_short_returns_single_chunk():
    out = split_message("short", limit=4096)
    assert out == ["short"]


def test_split_message_long_splits_on_newline_boundary():
    para = "\n".join(["A" * 100] * 50)   # 50 lines of 100 chars + newlines
    chunks = split_message(para, limit=500)
    assert len(chunks) > 1
    assert all(len(c) <= 500 for c in chunks)
    assert "\n".join(chunks).replace("\n\n", "\n").startswith("A" * 100)


def test_send_message_calls_telegram_api(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://api.telegram.org/bot-TOKEN-/sendMessage",
        method="POST",
        json={"ok": True, "result": {"message_id": 1}},
    )
    s = TelegramSender(bot_token="-TOKEN-", chat_id="12345")
    s.send("hello")
    req = httpx_mock.get_request()
    body = req.read().decode()
    assert "chat_id=12345" in body
    assert "text=hello" in body


def test_send_long_message_splits_into_multiple_calls(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://api.telegram.org/bot-TOKEN-/sendMessage",
        method="POST",
        json={"ok": True, "result": {"message_id": 1}},
    )
    s = TelegramSender(bot_token="-TOKEN-", chat_id="12345")
    text = ("X" * 4000 + "\n") * 3   # ~12000 chars, needs >=3 chunks
    s.send(text)
    reqs = httpx_mock.get_requests()
    assert len(reqs) >= 3
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
pytest tests/test_tg_sender.py -v
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement tg_sender**

Create `src/chat_daily_tg/tg_sender.py`:

```python
from __future__ import annotations
from dataclasses import dataclass
import httpx


def split_message(text: str, limit: int = 4096) -> list[str]:
    """Split text into <=limit chunks, preferring newline boundaries."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        # Try to split on last newline within limit
        cut = remaining.rfind("\n", 0, limit)
        if cut == -1 or cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut].rstrip("\n"))
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


@dataclass
class TelegramSender:
    bot_token: str
    chat_id: str
    timeout: float = 30.0

    def send(self, text: str, parse_mode: str | None = None) -> list[int]:
        """Send text (splitting if needed). Returns list of message_ids."""
        chunks = split_message(text)
        ids: list[int] = []
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        with httpx.Client(timeout=self.timeout) as c:
            for chunk in chunks:
                data = {"chat_id": self.chat_id, "text": chunk}
                if parse_mode:
                    data["parse_mode"] = parse_mode
                r = c.post(url, data=data)
                r.raise_for_status()
                body = r.json()
                if not body.get("ok"):
                    raise RuntimeError(f"Telegram API error: {body}")
                ids.append(body["result"]["message_id"])
        return ids
```

- [ ] **Step 4: Run tests, verify they pass**

```bash
pytest tests/test_tg_sender.py -v
```

Expected: 4 PASSED.

- [ ] **Step 5: Smoke-test by sending a real message**

```bash
python -c "
import os
from chat_daily_tg.tg_sender import TelegramSender
s = TelegramSender(bot_token=os.environ['TG_BOT_TOKEN'], chat_id=os.environ['TG_CHAT_ID'])
s.send('chat-daily-tg: tg_sender smoke test ✓')
"
```

Expected: message arrives in your Telegram chat with the bot.

- [ ] **Step 6: Commit**

```bash
git add src/chat_daily_tg/tg_sender.py tests/test_tg_sender.py
git commit -m "feat(tg_sender): Telegram Bot API client with message splitting"
```

---

### Task 1.6: Prompts module (summarization prompt v1)

**Files:**
- Create: `src/chat_daily_tg/prompts.py`

No unit tests — prompts are content, verified by smoke tests in Task 1.7.

- [ ] **Step 1: Write prompts module**

Create `src/chat_daily_tg/prompts.py`:

```python
from __future__ import annotations

SUMMARIZER_SYSTEM = """你是一个薅羊毛/理财/套利信息分析助手。
你的任务：对用户提供的多个微信群一天的聊天记录做结构化总结。

输出要求：两份 markdown + 一份 JSON，用三个 fence 分隔，顺序固定：

第一个 fence：
```markdown concise
(给 Telegram 手机端用的精简版，≤1500 字)
结构：
### 🗓️ 日期概览
(2-3 句话总述今天 N 个群的整体内容)

### 📌 值得关注
- 类型 | 内容 | 出处（群+人+时间）

### 🚨 死亡信号（若有）
- xxx 被标记为 dead（原文引用）

末尾附一行：详情：<path>
```

第二个 fence：
```markdown detailed
(给本地 md 档案的详细版，无长度限制)
结构：
## 群 1: <群名>
<2-3 句话总结 + 主干脉络>

## 群 2: ...

## 跨群合并话题
...

## 值得关注清单（完整表格）
...

## 人物画像
(主要贡献者的一句话评价)
```

第三个 fence：
```json opportunities
{
  "permanent_additions": [
    {
      "title": "...",
      "category": "invite_code|bank_product|activity|misc",
      "type": "permanent|product|activity",
      "content": "...",
      "url": null,
      "expires_at": null,
      "source_group": "...",
      "source_sender": "...",
      "notes": "..."
    }
  ],
  "hot_leads_additions": [
    {
      "title": "...",
      "summary": "2-3 行描述",
      "category": "arbitrage|bug|personal_trick|gray_zone",
      "source_group": "...",
      "source_sender": "...",
      "risk_notes": "..."
    }
  ],
  "death_signals": [
    {
      "target_title_or_id": "...",
      "signal_text": "...",
      "signal_source": "...",
      "confidence": "high|medium|low"
    }
  ]
}
```

严格遵守上面的 fence 顺序和格式，不要在 fence 之间写多余解释。
"""


def build_user_prompt(
    date: str,
    groups_with_content: list[tuple[str, str]],
    detail_path: str,
    active_permanent_summary: str = "",
    active_hot_leads_summary: str = "",
) -> str:
    """Build the user prompt.

    groups_with_content: list of (group_name, raw_markdown_export).
    detail_path: filesystem path to the detailed summary file (appended to concise version).
    active_permanent_summary / active_hot_leads_summary: existing DB context for death-signal detection.
    """
    groups_block = "\n\n".join(
        f"### === 群: {name} ===\n\n{content}" for name, content in groups_with_content
    )
    context = ""
    if active_permanent_summary or active_hot_leads_summary:
        context = f"""
## 当前活跃的机会（用于死亡信号检测）

### 永久库活跃条目
{active_permanent_summary or "(空)"}

### 热点板 14 天内活跃条目
{active_hot_leads_summary or "(空)"}
"""

    return f"""日期：{date}
详细版文件路径（精简版末尾要附这个路径）：{detail_path}

{context}

## 今日原始聊天记录

{groups_block}
"""
```

- [ ] **Step 2: Commit**

```bash
git add src/chat_daily_tg/prompts.py
git commit -m "feat(prompts): LLM summarization prompt v1"
```

---

### Task 1.7: Summarizer (parses LLM triple-fence output)

**Files:**
- Create: `src/chat_daily_tg/summarizer.py`
- Create: `tests/test_summarizer.py`

- [ ] **Step 1: Write failing tests for parsing**

Create `tests/test_summarizer.py`:

```python
from chat_daily_tg.summarizer import parse_summary_output, SummaryOutput


SAMPLE_OUTPUT = """```markdown concise
### 🗓️ 日期概览
Test concise
### 📌 值得关注
- item 1
```

```markdown detailed
## 群 1
Test detailed
```

```json opportunities
{
  "permanent_additions": [],
  "hot_leads_additions": [],
  "death_signals": []
}
```"""


def test_parse_summary_output_extracts_three_sections():
    out = parse_summary_output(SAMPLE_OUTPUT)
    assert isinstance(out, SummaryOutput)
    assert "Test concise" in out.concise_md
    assert "Test detailed" in out.detailed_md
    assert out.opportunities["permanent_additions"] == []


def test_parse_summary_output_missing_fence_raises():
    import pytest
    bad = "```markdown concise\nOnly one fence\n```"
    with pytest.raises(ValueError, match="detailed"):
        parse_summary_output(bad)
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
pytest tests/test_summarizer.py -v
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement summarizer**

Create `src/chat_daily_tg/summarizer.py`:

```python
from __future__ import annotations
from dataclasses import dataclass
import json
import re


@dataclass(frozen=True)
class SummaryOutput:
    concise_md: str
    detailed_md: str
    opportunities: dict


_FENCE_RE = re.compile(r"```(\w+)\s+(\w+)\n(.*?)```", re.DOTALL)


def parse_summary_output(text: str) -> SummaryOutput:
    """Parse the triple-fence LLM output into structured pieces.

    Expects fences in order: `markdown concise`, `markdown detailed`, `json opportunities`.
    """
    fences = {}
    for m in _FENCE_RE.finditer(text):
        lang, tag, body = m.group(1), m.group(2), m.group(3).strip()
        fences[(lang, tag)] = body
    required = [("markdown", "concise"), ("markdown", "detailed"), ("json", "opportunities")]
    for key in required:
        if key not in fences:
            raise ValueError(f"missing fence {key[0]} {key[1]}")
    opportunities = json.loads(fences[("json", "opportunities")])
    return SummaryOutput(
        concise_md=fences[("markdown", "concise")],
        detailed_md=fences[("markdown", "detailed")],
        opportunities=opportunities,
    )


def run_summary(
    llm_client,
    date: str,
    groups_with_content: list[tuple[str, str]],
    detail_path: str,
    active_permanent_summary: str = "",
    active_hot_leads_summary: str = "",
) -> SummaryOutput:
    """Call LLM with summarization prompts and parse result."""
    from chat_daily_tg.prompts import SUMMARIZER_SYSTEM, build_user_prompt

    user_prompt = build_user_prompt(
        date=date,
        groups_with_content=groups_with_content,
        detail_path=detail_path,
        active_permanent_summary=active_permanent_summary,
        active_hot_leads_summary=active_hot_leads_summary,
    )
    content, _usage = llm_client.chat(user_prompt, system=SUMMARIZER_SYSTEM)
    return parse_summary_output(content)
```

- [ ] **Step 4: Run tests, verify they pass**

```bash
pytest tests/test_summarizer.py -v
```

Expected: 2 PASSED.

- [ ] **Step 5: Commit**

```bash
git add src/chat_daily_tg/summarizer.py tests/test_summarizer.py
git commit -m "feat(summarizer): triple-fence LLM output parser + orchestration"
```

---

### Task 1.8: Daily orchestrator (`run_daily.py`) — MVP pipeline

**Files:**
- Create: `run_daily.py` (project root, per spec §7.2 launchd plist)
- Create: `tests/test_run_daily.py` (light integration-style)

- [ ] **Step 1: Write a smoke test that uses mock components**

Create `tests/test_run_daily.py`:

```python
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import date


def test_run_daily_pipeline_mocks(tmp_path, monkeypatch):
    """Mock wx_exporter, llm, tg, and verify orchestrator ties them together."""
    # Patch DATA_DIR to tmp
    import chat_daily_tg.paths as paths
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    monkeypatch.setattr(paths, "ARCHIVE_DIR", tmp_path / "archive")
    monkeypatch.setattr(paths, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(paths, "CONFIG_PATH", tmp_path / "config.yaml")
    import chat_daily_tg.archive as archive
    monkeypatch.setattr(archive, "ARCHIVE_DIR", tmp_path / "archive")

    # Write a minimal config
    (tmp_path / "config.yaml").write_text(
        """
groups: [G1]
llm: {endpoint: "http://x", model: "m", api_key_env: "K", max_tokens: 100}
telegram: {bot_token_env: "TT", chat_id_env: "TC"}
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("K", "fake")
    monkeypatch.setenv("TT", "fake")
    monkeypatch.setenv("TC", "123")

    with patch("chat_daily_tg.wx_exporter.subprocess.run") as run, \
         patch("chat_daily_tg.llm_client.httpx.Client") as llm_client_cls, \
         patch("chat_daily_tg.tg_sender.httpx.Client") as tg_client_cls:
        # wx subprocess
        run.return_value = MagicMock(returncode=0, stdout="已导出 3 条消息", stderr="")
        # llm returns a valid triple-fence
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{"message": {"content":
                "```markdown concise\nConcise\n```\n\n```markdown detailed\nDetailed\n```\n\n```json opportunities\n{\"permanent_additions\":[],\"hot_leads_additions\":[],\"death_signals\":[]}\n```"
            }}],
            "usage": {"total_tokens": 10},
        }
        mock_resp.raise_for_status = MagicMock()
        llm_client_cls.return_value.__enter__.return_value.post.return_value = mock_resp
        # tg sender
        tg_resp = MagicMock()
        tg_resp.json.return_value = {"ok": True, "result": {"message_id": 1}}
        tg_resp.raise_for_status = MagicMock()
        tg_client_cls.return_value.__enter__.return_value.post.return_value = tg_resp

        # Also patch export_group's group file write
        def fake_run(cmd, **kw):
            # find -o path
            i = cmd.index("-o")
            Path(cmd[i+1]).parent.mkdir(parents=True, exist_ok=True)
            Path(cmd[i+1]).write_text("# group export\nsample\n", encoding="utf-8")
            return MagicMock(returncode=0, stdout="已导出 3 条消息", stderr="")
        run.side_effect = fake_run

        from run_daily import main
        rc = main(date_str="2026-04-17")
        assert rc == 0

    # Verify archive file was written
    assert (tmp_path / "archive" / "2026" / "04" / "17" / "summary.md").exists()
```

- [ ] **Step 2: Run test, verify it fails**

```bash
pytest tests/test_run_daily.py -v
```

Expected: FAIL — `run_daily` module missing.

- [ ] **Step 3: Implement orchestrator**

Create `/Users/Apple/projects/chat-daily-tg/run_daily.py`:

```python
"""Entry point for chat-daily-tg. Run once per day at 08:00 local time."""
from __future__ import annotations
import argparse
from datetime import date, timedelta
import os
import sys

from chat_daily_tg.archive import safe_filename, prepare_archive_day
from chat_daily_tg.config import load_config
from chat_daily_tg.llm_client import LLMClient
from chat_daily_tg.paths import CONFIG_PATH
from chat_daily_tg.summarizer import run_summary
from chat_daily_tg.tg_sender import TelegramSender


def yesterday_iso() -> str:
    return (date.today() - timedelta(days=1)).isoformat()


def main(date_str: str | None = None) -> int:
    if date_str is None:
        date_str = yesterday_iso()
    next_day = (date.fromisoformat(date_str) + timedelta(days=1)).isoformat()

    cfg = load_config(CONFIG_PATH)

    # 1. Export each group
    archive_dir = prepare_archive_day(date_str)
    groups_with_content: list[tuple[str, str]] = []
    for group in cfg.groups:
        out_path = archive_dir / f"{safe_filename(group)}.md"
        from chat_daily_tg.wx_exporter import export_group
        try:
            result = export_group(
                group_name=group, since=date_str, until=next_day, out_path=out_path,
            )
            print(f"[export] {group}: {result.message_count} msgs → {out_path}")
        except Exception as e:
            print(f"[export][WARN] {group}: {e}", file=sys.stderr)
            continue
        content = out_path.read_text(encoding="utf-8")
        if content.strip():
            groups_with_content.append((group, content))

    if not groups_with_content:
        print("[run_daily] no content exported, aborting", file=sys.stderr)
        return 1

    # 2. LLM summarize
    api_key = os.environ[cfg.llm.api_key_env]
    llm = LLMClient(
        endpoint=cfg.llm.endpoint,
        model=cfg.llm.model,
        api_key=api_key,
        max_tokens=cfg.llm.max_tokens,
    )
    detail_path = str(archive_dir / "summary.md")
    out = run_summary(
        llm_client=llm,
        date=date_str,
        groups_with_content=groups_with_content,
        detail_path=detail_path,
    )

    # 3. Write detailed archive
    (archive_dir / "summary.md").write_text(out.detailed_md, encoding="utf-8")

    # 4. Push Telegram
    bot_token = os.environ[cfg.telegram.bot_token_env]
    chat_id = os.environ[cfg.telegram.chat_id_env]
    tg = TelegramSender(bot_token=bot_token, chat_id=chat_id)
    tg.send(out.concise_md)

    print(f"[run_daily] ✓ complete for {date_str}")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--date", help="YYYY-MM-DD (default: yesterday)", default=None)
    args = p.parse_args()
    sys.exit(main(date_str=args.date))
```

- [ ] **Step 4: Run test, verify it passes**

```bash
pytest tests/test_run_daily.py -v
```

Expected: 1 PASSED.

- [ ] **Step 5: End-to-end smoke test on real data (2026-04-17)**

```bash
cd /Users/Apple/projects/chat-daily-tg
source venv/bin/activate
python run_daily.py --date 2026-04-17
```

Expected behaviors:
- Prints `[export] 贝利知识星球VIP群❤️: N msgs → ...` for each group
- Writes `~/chat-daily/archive/2026/04/17/<group>.md` for each group
- Writes `~/chat-daily/archive/2026/04/17/summary.md` with detailed summary
- Sends a Telegram message to `@Taoli98Bot` with concise summary
- Exits 0

Verify:

```bash
ls -la ~/chat-daily/archive/2026/04/17/
```

- [ ] **Step 6: Commit**

```bash
git add run_daily.py tests/test_run_daily.py
git commit -m "feat(orchestrator): MVP run_daily pipeline with archive + TG push"
```

---

## Phase 2 — Automation (retry, logging, launchd)

### Task 2.1: Retry wrapper using tenacity

**Files:**
- Modify: `src/chat_daily_tg/llm_client.py` (add retry)
- Modify: `src/chat_daily_tg/tg_sender.py` (add retry)
- Create: `tests/test_retry.py`

- [ ] **Step 1: Write failing test for retry on 500**

Create `tests/test_retry.py`:

```python
from pytest_httpx import HTTPXMock
import pytest
from chat_daily_tg.llm_client import LLMClient


def test_llm_client_retries_on_500(httpx_mock: HTTPXMock):
    # Two 500s then a success
    httpx_mock.add_response(url="http://127.0.0.1:8317/v1/chat/completions",
                             method="POST", status_code=500)
    httpx_mock.add_response(url="http://127.0.0.1:8317/v1/chat/completions",
                             method="POST", status_code=500)
    httpx_mock.add_response(url="http://127.0.0.1:8317/v1/chat/completions",
                             method="POST",
                             json={"choices":[{"message":{"content":"ok"}}],"usage":{}})
    c = LLMClient(
        endpoint="http://127.0.0.1:8317/v1",
        model="m", api_key="k", max_tokens=10,
        retry_max_attempts=3, retry_backoff_seconds=[0, 0, 0],
    )
    text, _ = c.chat("hi")
    assert text == "ok"
    assert len(httpx_mock.get_requests()) == 3
```

- [ ] **Step 2: Run test, verify it fails**

```bash
pytest tests/test_retry.py -v
```

Expected: FAIL — `LLMClient.__init__` takes no `retry_max_attempts`.

- [ ] **Step 3: Add retry support to LLMClient**

Modify `src/chat_daily_tg/llm_client.py`:

```python
from __future__ import annotations
from dataclasses import dataclass, field
import httpx
from tenacity import (
    retry, stop_after_attempt, wait_fixed, retry_if_exception_type,
    before_sleep_log,
)
import logging

log = logging.getLogger(__name__)


@dataclass
class LLMClient:
    endpoint: str
    model: str
    api_key: str
    max_tokens: int = 8000
    timeout: float = 120.0
    retry_max_attempts: int = 3
    retry_backoff_seconds: list = field(default_factory=lambda: [5, 15, 60])

    def chat(self, prompt: str, system: str | None = None) -> tuple[str, dict]:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        # Build a fresh tenacity decorator using instance config
        attempts_iter = iter(self.retry_backoff_seconds + [0] * 10)

        def _do_request():
            with httpx.Client(timeout=self.timeout) as client:
                r = client.post(
                    f"{self.endpoint}/chat/completions",
                    json=payload,
                    headers=headers,
                )
                r.raise_for_status()
                return r.json()

        attempts = 0
        last_exc = None
        for wait in self.retry_backoff_seconds[: self.retry_max_attempts]:
            attempts += 1
            try:
                data = _do_request()
                content = data["choices"][0]["message"]["content"]
                usage = data.get("usage", {})
                return content, usage
            except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.ConnectError) as e:
                last_exc = e
                log.warning("llm call failed (attempt %d): %s", attempts, e)
                if attempts < self.retry_max_attempts:
                    import time
                    time.sleep(wait)
        assert last_exc is not None
        raise last_exc
```

Similarly modify `src/chat_daily_tg/tg_sender.py`:

```python
from __future__ import annotations
from dataclasses import dataclass, field
import httpx
import logging
import time

log = logging.getLogger(__name__)


def split_message(text: str, limit: int = 4096) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut == -1 or cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut].rstrip("\n"))
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


@dataclass
class TelegramSender:
    bot_token: str
    chat_id: str
    timeout: float = 30.0
    retry_max_attempts: int = 3
    retry_backoff_seconds: list = field(default_factory=lambda: [5, 15, 60])

    def _send_one(self, text: str, parse_mode: str | None = None) -> int:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        data = {"chat_id": self.chat_id, "text": text}
        if parse_mode:
            data["parse_mode"] = parse_mode

        last_exc = None
        for attempt, wait in enumerate(
            self.retry_backoff_seconds[: self.retry_max_attempts], start=1
        ):
            try:
                with httpx.Client(timeout=self.timeout) as c:
                    r = c.post(url, data=data)
                    r.raise_for_status()
                    body = r.json()
                    if not body.get("ok"):
                        raise RuntimeError(f"Telegram API error: {body}")
                    return body["result"]["message_id"]
            except Exception as e:
                last_exc = e
                log.warning("tg send failed (attempt %d): %s", attempt, e)
                if attempt < self.retry_max_attempts:
                    time.sleep(wait)
        assert last_exc is not None
        raise last_exc

    def send(self, text: str, parse_mode: str | None = None) -> list[int]:
        chunks = split_message(text)
        return [self._send_one(c, parse_mode) for c in chunks]
```

- [ ] **Step 4: Run retry test**

```bash
pytest tests/test_retry.py -v
```

Expected: 1 PASSED.

- [ ] **Step 5: Run all tests to ensure no regression**

```bash
pytest
```

Expected: all prior tests still pass (~15 tests).

- [ ] **Step 6: Commit**

```bash
git add src/chat_daily_tg/llm_client.py src/chat_daily_tg/tg_sender.py tests/test_retry.py
git commit -m "feat(retry): tenacity-based retry for LLM and TG clients"
```

---

### Task 2.2: macOS notifier

**Files:**
- Create: `src/chat_daily_tg/notifier.py`
- Create: `tests/test_notifier.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_notifier.py`:

```python
from unittest.mock import patch
from chat_daily_tg.notifier import notify_failure


def test_notify_failure_calls_osascript():
    with patch("chat_daily_tg.notifier.subprocess.run") as run:
        notify_failure(title="chat-daily-tg 失败", message="pipeline 异常")
        called = run.call_args[0][0]
        assert called[0] == "osascript"
        joined = " ".join(called)
        assert "display notification" in joined
        assert "chat-daily-tg 失败" in joined
        assert "pipeline 异常" in joined
```

- [ ] **Step 2: Run test, verify fail**

```bash
pytest tests/test_notifier.py -v
```

Expected: FAIL.

- [ ] **Step 3: Implement notifier**

Create `src/chat_daily_tg/notifier.py`:

```python
from __future__ import annotations
import subprocess


def notify_failure(title: str, message: str) -> None:
    """Show a macOS notification via osascript. No-ops if osascript missing."""
    # Escape double quotes in user text
    safe_title = title.replace('"', '\\"')
    safe_msg = message.replace('"', '\\"')
    script = f'display notification "{safe_msg}" with title "{safe_title}"'
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True)
```

- [ ] **Step 4: Run test, verify pass**

```bash
pytest tests/test_notifier.py -v
```

Expected: 1 PASSED.

- [ ] **Step 5: Live test**

```bash
python -c "from chat_daily_tg.notifier import notify_failure; notify_failure('test', 'hello from chat-daily-tg')"
```

Expected: macOS notification banner appears briefly.

- [ ] **Step 6: Commit**

```bash
git add src/chat_daily_tg/notifier.py tests/test_notifier.py
git commit -m "feat(notifier): macOS notification via osascript"
```

---

### Task 2.3: Logging setup + integrate into run_daily

**Files:**
- Create: `src/chat_daily_tg/logging_setup.py`
- Modify: `run_daily.py`

- [ ] **Step 1: Write logging setup**

Create `src/chat_daily_tg/logging_setup.py`:

```python
from __future__ import annotations
import logging
from pathlib import Path


def configure_logging(log_file: Path, level: int = logging.INFO) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)s %(name)s %(message)s"
    handlers: list[logging.Handler] = [
        logging.StreamHandler(),
        logging.FileHandler(log_file, encoding="utf-8"),
    ]
    logging.basicConfig(level=level, format=fmt, handlers=handlers, force=True)
```

- [ ] **Step 2: Integrate into run_daily.py — wrap main in try/except, log everything**

Modify `run_daily.py`:

```python
"""Entry point for chat-daily-tg. Run once per day at 08:00 local time."""
from __future__ import annotations
import argparse
from datetime import date, timedelta
import logging
import os
import sys

from chat_daily_tg.archive import safe_filename, prepare_archive_day
from chat_daily_tg.config import load_config
from chat_daily_tg.llm_client import LLMClient
from chat_daily_tg.logging_setup import configure_logging
from chat_daily_tg.notifier import notify_failure
from chat_daily_tg.paths import CONFIG_PATH, log_file_for
from chat_daily_tg.summarizer import run_summary
from chat_daily_tg.tg_sender import TelegramSender

log = logging.getLogger("run_daily")


def yesterday_iso() -> str:
    return (date.today() - timedelta(days=1)).isoformat()


def main(date_str: str | None = None) -> int:
    if date_str is None:
        date_str = yesterday_iso()
    configure_logging(log_file_for(date_str))
    try:
        return _run(date_str)
    except Exception as e:
        log.exception("pipeline failed: %s", e)
        notify_failure("chat-daily-tg 失败", f"{type(e).__name__}: {e}\n日志: {log_file_for(date_str)}")
        return 1


def _run(date_str: str) -> int:
    next_day = (date.fromisoformat(date_str) + timedelta(days=1)).isoformat()
    cfg = load_config(CONFIG_PATH)
    log.info("config loaded: %d groups, model=%s", len(cfg.groups), cfg.llm.model)

    archive_dir = prepare_archive_day(date_str)
    groups_with_content: list[tuple[str, str]] = []
    for group in cfg.groups:
        out_path = archive_dir / f"{safe_filename(group)}.md"
        from chat_daily_tg.wx_exporter import export_group
        try:
            result = export_group(
                group_name=group, since=date_str, until=next_day, out_path=out_path,
            )
            log.info("exported %s: %d msgs", group, result.message_count)
        except Exception as e:
            log.warning("export failed for %s: %s", group, e)
            continue
        content = out_path.read_text(encoding="utf-8")
        if content.strip():
            groups_with_content.append((group, content))

    if not groups_with_content:
        log.error("no content exported, aborting")
        return 1

    api_key = os.environ[cfg.llm.api_key_env]
    llm = LLMClient(
        endpoint=cfg.llm.endpoint, model=cfg.llm.model, api_key=api_key,
        max_tokens=cfg.llm.max_tokens,
        retry_max_attempts=cfg.retry.max_attempts,
        retry_backoff_seconds=cfg.retry.backoff_seconds,
    )
    detail_path = str(archive_dir / "summary.md")
    log.info("calling LLM for summary…")
    out = run_summary(
        llm_client=llm, date=date_str,
        groups_with_content=groups_with_content, detail_path=detail_path,
    )
    log.info("LLM returned: concise=%d chars, detailed=%d chars",
             len(out.concise_md), len(out.detailed_md))

    (archive_dir / "summary.md").write_text(out.detailed_md, encoding="utf-8")

    bot_token = os.environ[cfg.telegram.bot_token_env]
    chat_id = os.environ[cfg.telegram.chat_id_env]
    tg = TelegramSender(
        bot_token=bot_token, chat_id=chat_id,
        retry_max_attempts=cfg.retry.max_attempts,
        retry_backoff_seconds=cfg.retry.backoff_seconds,
    )
    tg.send(out.concise_md)
    log.info("TG push complete")

    log.info("✓ run_daily complete for %s", date_str)
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--date", help="YYYY-MM-DD (default: yesterday)", default=None)
    args = p.parse_args()
    sys.exit(main(date_str=args.date))
```

- [ ] **Step 3: Rerun full end-to-end with logging**

```bash
python run_daily.py --date 2026-04-17
```

Expected: log lines prefixed with timestamps; `~/chat-daily/logs/2026-04-17.log` file written.

- [ ] **Step 4: Test failure path**

```bash
CLIPROXY_API_KEY="bad-key" python run_daily.py --date 2026-04-17
```

Expected: pipeline fails, macOS notification banner appears, `~/chat-daily/logs/2026-04-17.log` has exception stacktrace, exit code 1.

- [ ] **Step 5: Commit**

```bash
git add src/chat_daily_tg/logging_setup.py run_daily.py
git commit -m "feat(logging): structured logging + failure notification path"
```

---

### Task 2.4: launchd plist + installer script

**Files:**
- Create: `launchd/com.apple.chat-daily-tg.plist`
- Create: `scripts/install-launchd.sh`

- [ ] **Step 1: Write plist template**

Create `/Users/Apple/projects/chat-daily-tg/launchd/com.apple.chat-daily-tg.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.apple.chat-daily-tg</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/Apple/projects/chat-daily-tg/venv/bin/python</string>
        <string>/Users/Apple/projects/chat-daily-tg/run_daily.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/Apple/projects/chat-daily-tg</string>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>8</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>/Users/Apple/chat-daily/logs/stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/Apple/chat-daily/logs/stderr.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>CLIPROXY_API_KEY</key>
        <string>REPLACE_WITH_REAL_KEY</string>
        <key>TG_BOT_TOKEN</key>
        <string>REPLACE_WITH_REAL_TOKEN</string>
        <key>TG_CHAT_ID</key>
        <string>REPLACE_WITH_REAL_CHAT_ID</string>
    </dict>
</dict>
</plist>
```

- [ ] **Step 2: Write installer script that fills in secrets from env and deploys**

Create `/Users/Apple/projects/chat-daily-tg/scripts/install-launchd.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

: "${CLIPROXY_API_KEY:?Set CLIPROXY_API_KEY env var before running}"
: "${TG_BOT_TOKEN:?Set TG_BOT_TOKEN env var before running}"
: "${TG_CHAT_ID:?Set TG_CHAT_ID env var before running}"

PROJECT=/Users/Apple/projects/chat-daily-tg
SRC="$PROJECT/launchd/com.apple.chat-daily-tg.plist"
DST="$HOME/Library/LaunchAgents/com.apple.chat-daily-tg.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/chat-daily/logs"

# Render plist with secrets inlined
sed \
  -e "s|REPLACE_WITH_REAL_KEY|$CLIPROXY_API_KEY|" \
  -e "s|REPLACE_WITH_REAL_TOKEN|$TG_BOT_TOKEN|" \
  -e "s|REPLACE_WITH_REAL_CHAT_ID|$TG_CHAT_ID|" \
  "$SRC" > "$DST"

# Load (unload first to avoid "already loaded" errors)
launchctl unload "$DST" 2>/dev/null || true
launchctl load "$DST"

echo "✓ launchd agent loaded: $DST"
launchctl list | grep chat-daily-tg
```

- [ ] **Step 3: Make script executable, verify syntax**

```bash
chmod +x /Users/Apple/projects/chat-daily-tg/scripts/install-launchd.sh
bash -n /Users/Apple/projects/chat-daily-tg/scripts/install-launchd.sh
```

Expected: no output (syntax OK).

- [ ] **Step 4: Run installer (loads launchd agent)**

```bash
cd /Users/Apple/projects/chat-daily-tg
source ~/.zshenv    # ensure env vars present
./scripts/install-launchd.sh
```

Expected: prints `✓ launchd agent loaded` + a line like `- 0 com.apple.chat-daily-tg`.

- [ ] **Step 5: Trigger manually via launchctl to verify**

```bash
launchctl start com.apple.chat-daily-tg
# wait 30s
ls -la ~/chat-daily/logs/
cat ~/chat-daily/logs/stdout.log | tail -30
```

Expected: log shows today's run (it will process yesterday's data), TG message arrives.

- [ ] **Step 6: Commit**

```bash
cd /Users/Apple/projects/chat-daily-tg
git add launchd/ scripts/
git commit -m "feat(launchd): daily schedule via launchd + installer script"
```

---

## Phase 3 — Fingerprinting, dedup, DB

### Task 3.1: Fingerprint extractors

**Files:**
- Create: `src/chat_daily_tg/fingerprint.py`
- Create: `tests/test_fingerprint.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_fingerprint.py`:

```python
from chat_daily_tg.fingerprint import (
    extract_urls, extract_invite_codes, extract_md5s, extract_phones,
)


def test_extract_urls_basic():
    t = "check https://example.com/a?b=1 and http://x.y/z"
    urls = extract_urls(t)
    assert "https://example.com/a?b=1" in urls
    assert "http://x.y/z" in urls


def test_extract_urls_filters_cdn_and_image_hosts():
    t = "https://snsvideo.hk.wechat.com/foo and https://app.okx.com/en-us/join/49084340"
    urls = extract_urls(t)
    assert "https://app.okx.com/en-us/join/49084340" in urls
    assert "https://snsvideo.hk.wechat.com/foo" not in urls


def test_extract_invite_codes_with_context():
    t1 = "邀请码 PjGhDx 护照可注册"
    codes = extract_invite_codes(t1)
    assert "PjGhDx" in codes

    t2 = "这只是一段有大写BigWord和数字12345的无关文本"
    codes = extract_invite_codes(t2)
    assert codes == []


def test_extract_md5s_from_xml():
    xml = '<emoticonmd5>e61a83644f703e32377a4fe4f1d12fdb</emoticonmd5><cdnthumbmd5>58f01cd3c2f35f0dfdd29c8966fa98b1</cdnthumbmd5>'
    md5s = extract_md5s(xml)
    assert "e61a83644f703e32377a4fe4f1d12fdb" in md5s
    assert "58f01cd3c2f35f0dfdd29c8966fa98b1" in md5s


def test_extract_phones():
    t = "call 13812345678 or 15987654321, not 12345"
    phones = extract_phones(t)
    assert "13812345678" in phones
    assert "15987654321" in phones
    assert "12345" not in phones
```

- [ ] **Step 2: Run, verify fail**

```bash
pytest tests/test_fingerprint.py -v
```

Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

Create `src/chat_daily_tg/fingerprint.py`:

```python
from __future__ import annotations
import re

_URL_RE = re.compile(r"https?://[^\s<>\"')]+")
_CDN_BLACKLIST = [
    "snsvideo.", "vweixinf.tc.qq.com", ".wx.qq.com/cgi-bin/", ".wechat.com/110",
    "emoji", "sticker", "/cdn/", "mmsns.qpic.cn",
]
_INVITE_CONTEXT_RE = re.compile(
    r"(?:邀请码|推荐码|invite\s*code|referral|邀请\s*[:：])\s*[：:]?\s*([A-Za-z0-9]{5,12})",
    re.IGNORECASE,
)
_MD5_RE = re.compile(r"<(?:emoticon|cdnthumb|file)?md5>([0-9a-fA-F]{32})</")
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")


def extract_urls(text: str) -> list[str]:
    out = []
    for m in _URL_RE.finditer(text):
        u = m.group(0).rstrip(".,，。;：:")
        if any(b in u for b in _CDN_BLACKLIST):
            continue
        out.append(u)
    return out


def extract_invite_codes(text: str) -> list[str]:
    return [m.group(1) for m in _INVITE_CONTEXT_RE.finditer(text)]


def extract_md5s(text: str) -> list[str]:
    return [m.group(1).lower() for m in _MD5_RE.finditer(text)]


def extract_phones(text: str) -> list[str]:
    return _PHONE_RE.findall(text)


def fingerprints_for(text: str) -> dict[str, list[str]]:
    """Return a dict of all fingerprint types found."""
    return {
        "urls": extract_urls(text),
        "invite_codes": extract_invite_codes(text),
        "md5s": extract_md5s(text),
        "phones": extract_phones(text),
    }
```

- [ ] **Step 4: Run, verify pass**

```bash
pytest tests/test_fingerprint.py -v
```

Expected: 5 PASSED.

- [ ] **Step 5: Commit**

```bash
git add src/chat_daily_tg/fingerprint.py tests/test_fingerprint.py
git commit -m "feat(fingerprint): URL/invite/MD5/phone extractors"
```

---

### Task 3.2: Cross-group deduplication

**Files:**
- Create: `src/chat_daily_tg/dedup.py`
- Create: `tests/test_dedup.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_dedup.py`:

```python
from chat_daily_tg.dedup import find_cross_group_dupes, DedupKey


def test_same_url_in_two_groups_is_dupe():
    msgs = [
        {"group": "G1", "sender": "A", "time": "10:00",
         "content": "看这个 https://example.com/offer"},
        {"group": "G2", "sender": "A", "time": "10:05",
         "content": "https://example.com/offer 源头"},
    ]
    groups = find_cross_group_dupes(msgs)
    assert len(groups) == 1
    assert groups[0].key == DedupKey(kind="url", value="https://example.com/offer")
    assert {m["group"] for m in groups[0].messages} == {"G1", "G2"}


def test_different_urls_no_dupe():
    msgs = [
        {"group": "G1", "sender": "A", "time": "10:00",
         "content": "https://a.example"},
        {"group": "G2", "sender": "B", "time": "10:05",
         "content": "https://b.example"},
    ]
    assert find_cross_group_dupes(msgs) == []


def test_long_text_content_hash_matches():
    long = "X" * 100
    msgs = [
        {"group": "G1", "sender": "A", "time": "10:00", "content": long},
        {"group": "G2", "sender": "A", "time": "10:01", "content": long},
    ]
    groups = find_cross_group_dupes(msgs)
    assert len(groups) == 1
    assert groups[0].key.kind == "content_hash"


def test_short_text_not_deduped():
    msgs = [
        {"group": "G1", "sender": "A", "time": "10:00", "content": "好的"},
        {"group": "G2", "sender": "B", "time": "10:01", "content": "好的"},
    ]
    assert find_cross_group_dupes(msgs) == []
```

- [ ] **Step 2: Run, verify fail**

```bash
pytest tests/test_dedup.py -v
```

Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

Create `src/chat_daily_tg/dedup.py`:

```python
from __future__ import annotations
from dataclasses import dataclass
from hashlib import sha256
from typing import Any
from chat_daily_tg.fingerprint import fingerprints_for


SHORT_TEXT_THRESHOLD = 30


@dataclass(frozen=True)
class DedupKey:
    kind: str      # "url" | "invite_code" | "md5" | "phone" | "content_hash"
    value: str


@dataclass(frozen=True)
class DupeGroup:
    key: DedupKey
    messages: list[dict]


def _content_hash(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def find_cross_group_dupes(messages: list[dict]) -> list[DupeGroup]:
    """Return list of DupeGroup where each group has messages from ≥2 distinct groups.

    Input message dicts must have keys: group, sender, time, content.
    """
    # Map key → list of messages
    key_to_msgs: dict[DedupKey, list[dict]] = {}

    def _add(key: DedupKey, msg: dict):
        key_to_msgs.setdefault(key, []).append(msg)

    for m in messages:
        content = m.get("content", "")
        fps = fingerprints_for(content)
        for url in fps["urls"]:
            _add(DedupKey("url", url), m)
        for code in fps["invite_codes"]:
            _add(DedupKey("invite_code", code), m)
        for md5 in fps["md5s"]:
            _add(DedupKey("md5", md5), m)
        for phone in fps["phones"]:
            _add(DedupKey("phone", phone), m)
        # Content hash only for long text
        if len(content) > SHORT_TEXT_THRESHOLD:
            _add(DedupKey("content_hash", _content_hash(content)), m)

    # Keep only groups with messages from ≥2 distinct `group` values
    out: list[DupeGroup] = []
    for key, msgs in key_to_msgs.items():
        groups = {m["group"] for m in msgs}
        if len(groups) >= 2:
            out.append(DupeGroup(key=key, messages=msgs))
    return out
```

- [ ] **Step 4: Run, verify pass**

```bash
pytest tests/test_dedup.py -v
```

Expected: 4 PASSED.

- [ ] **Step 5: Commit**

```bash
git add src/chat_daily_tg/dedup.py tests/test_dedup.py
git commit -m "feat(dedup): cross-group fingerprint + content-hash deduplication"
```

---

### Task 3.3: permanent.jsonl CRUD

**Files:**
- Create: `src/chat_daily_tg/db.py`
- Create: `tests/test_db.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_db.py`:

```python
from pathlib import Path
from chat_daily_tg.db import PermanentDB, PermanentEntry


def test_append_and_read(tmp_path: Path):
    db = PermanentDB(path=tmp_path / "permanent.jsonl")
    e = PermanentEntry(
        id="2026-04-17-foo",
        captured_at="2026-04-17T10:00:00+08:00",
        source_group="G1",
        source_sender="Alice",
        category="invite_code",
        type="permanent",
        title="Foo invite",
        content="ABC123",
    )
    db.append(e)
    entries = list(db.read_all())
    assert len(entries) == 1
    assert entries[0].id == "2026-04-17-foo"


def test_mark_dead(tmp_path: Path):
    db = PermanentDB(path=tmp_path / "permanent.jsonl")
    db.append(PermanentEntry(
        id="e1", captured_at="2026-04-17", source_group="G", source_sender="A",
        category="invite_code", type="permanent", title="t", content="c",
    ))
    db.append(PermanentEntry(
        id="e2", captured_at="2026-04-17", source_group="G", source_sender="A",
        category="invite_code", type="permanent", title="t2", content="c2",
    ))
    db.mark_status("e1", status="dead", death_signal="关门了")
    entries = list(db.read_all())
    e1 = next(e for e in entries if e.id == "e1")
    e2 = next(e for e in entries if e.id == "e2")
    assert e1.status == "dead"
    assert e1.death_signal == "关门了"
    assert e2.status == "alive"


def test_find_by_id_returns_none_if_missing(tmp_path: Path):
    db = PermanentDB(path=tmp_path / "permanent.jsonl")
    assert db.find("nonexistent") is None
```

- [ ] **Step 2: Run, verify fail**

```bash
pytest tests/test_db.py -v
```

Expected: FAIL.

- [ ] **Step 3: Implement**

Create `src/chat_daily_tg/db.py`:

```python
from __future__ import annotations
from dataclasses import dataclass, field, asdict
import json
from pathlib import Path
from typing import Iterator, Literal


Category = Literal["invite_code", "bank_product", "activity", "misc"]
EntryType = Literal["permanent", "product", "activity"]
Status = Literal["alive", "likely_dead", "dead", "unknown"]


@dataclass
class PermanentEntry:
    id: str
    captured_at: str
    source_group: str
    source_sender: str
    category: Category
    type: EntryType
    title: str
    content: str
    url: str | None = None
    expires_at: str | None = None
    last_mentioned_at: str | None = None
    mention_count: int = 1
    status: Status = "alive"
    death_signal: str | None = None
    notes: str | None = None


@dataclass
class PermanentDB:
    path: Path

    def _ensure(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()

    def read_all(self) -> Iterator[PermanentEntry]:
        if not self.path.exists():
            return
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                yield PermanentEntry(**data)

    def append(self, entry: PermanentEntry) -> None:
        self._ensure()
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")

    def find(self, entry_id: str) -> PermanentEntry | None:
        for e in self.read_all():
            if e.id == entry_id:
                return e
        return None

    def mark_status(
        self, entry_id: str, status: Status, death_signal: str | None = None
    ) -> bool:
        """Rewrite file with updated status for entry_id. Returns True if found."""
        entries = list(self.read_all())
        found = False
        for e in entries:
            if e.id == entry_id:
                e.status = status
                if death_signal is not None:
                    e.death_signal = death_signal
                found = True
        if found:
            with open(self.path, "w", encoding="utf-8") as f:
                for e in entries:
                    f.write(json.dumps(asdict(e), ensure_ascii=False) + "\n")
        return found
```

- [ ] **Step 4: Run, verify pass**

```bash
pytest tests/test_db.py -v
```

Expected: 3 PASSED.

- [ ] **Step 5: Commit**

```bash
git add src/chat_daily_tg/db.py tests/test_db.py
git commit -m "feat(db): permanent.jsonl CRUD with rewrite-on-update"
```

---

### Task 3.4: permanent.md regeneration from JSONL

**Files:**
- Create: `src/chat_daily_tg/permanent_md.py`
- Create: `tests/test_permanent_md.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_permanent_md.py`:

```python
from pathlib import Path
from chat_daily_tg.db import PermanentDB, PermanentEntry
from chat_daily_tg.permanent_md import regenerate_permanent_md


def test_regenerate_groups_by_category(tmp_path: Path):
    db_path = tmp_path / "p.jsonl"
    md_path = tmp_path / "p.md"
    db = PermanentDB(db_path)
    db.append(PermanentEntry(
        id="e1", captured_at="2026-04-17T10:00", source_group="G1", source_sender="A",
        category="invite_code", type="permanent", title="Bitget invite", content="CODE",
    ))
    db.append(PermanentEntry(
        id="e2", captured_at="2026-04-17T11:00", source_group="G1", source_sender="B",
        category="bank_product", type="product", title="恒生线上开户", content="CNID",
    ))
    regenerate_permanent_md(db_path, md_path)

    text = md_path.read_text(encoding="utf-8")
    assert "# 永久机会库" in text
    assert "## invite_code" in text or "## 邀请码" in text
    assert "Bitget invite" in text
    assert "恒生线上开户" in text
    assert "e1" in text  # id is referenced
```

- [ ] **Step 2: Run, verify fail**

```bash
pytest tests/test_permanent_md.py -v
```

Expected: FAIL.

- [ ] **Step 3: Implement**

Create `src/chat_daily_tg/permanent_md.py`:

```python
from __future__ import annotations
from collections import defaultdict
from pathlib import Path
from chat_daily_tg.db import PermanentDB


CATEGORY_LABELS = {
    "invite_code": "邀请码 / 推荐码",
    "bank_product": "银行 / 金融产品",
    "activity": "活动 / 优惠",
    "misc": "其他",
}


def regenerate_permanent_md(db_path: Path, md_path: Path) -> None:
    db = PermanentDB(db_path)
    by_cat: dict[str, list] = defaultdict(list)
    for e in db.read_all():
        by_cat[e.category].append(e)

    lines = ["# 永久机会库", "", "> 此文件由脚本自动生成，不要手动编辑。改 `permanent.jsonl`。", ""]
    for cat, label in CATEGORY_LABELS.items():
        entries = sorted(by_cat.get(cat, []), key=lambda e: e.captured_at, reverse=True)
        if not entries:
            continue
        lines.append(f"## {label}")
        lines.append("")
        lines.append("| 状态 | 标题 | 内容 | 来源 | 抓取时间 | ID |")
        lines.append("|---|---|---|---|---|---|")
        for e in entries:
            status_icon = {"alive": "✅", "likely_dead": "⚠️", "dead": "💀", "unknown": "❓"}.get(e.status, "?")
            row = (
                f"| {status_icon} {e.status} | {e.title} | {e.content} "
                f"| {e.source_group} / {e.source_sender} | {e.captured_at} | `{e.id}` |"
            )
            lines.append(row)
        lines.append("")

    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text("\n".join(lines), encoding="utf-8")
```

- [ ] **Step 4: Run, verify pass**

```bash
pytest tests/test_permanent_md.py -v
```

Expected: 1 PASSED.

- [ ] **Step 5: Commit**

```bash
git add src/chat_daily_tg/permanent_md.py tests/test_permanent_md.py
git commit -m "feat(permanent_md): regenerate human-readable view from JSONL"
```

---

### Task 3.5: Hot-leads board with 14-day roll-off

**Files:**
- Create: `src/chat_daily_tg/hot_leads.py`
- Create: `tests/test_hot_leads.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_hot_leads.py`:

```python
from datetime import date, timedelta
from pathlib import Path
from chat_daily_tg.hot_leads import (
    HotLead, append_day_leads, regenerate_latest, load_all_leads,
)


def test_append_only_creates_file_when_nonempty(tmp_path: Path):
    leads = [HotLead(
        id="hl-1", captured_at="2026-04-17", title="OpenAI low plus",
        summary="86GameStore source", category="arbitrage",
        source_group="G1", source_sender="Alice", status="alive",
    )]
    p = append_day_leads(tmp_path, "2026-04-17", leads)
    assert p is not None
    assert p.exists()
    assert "OpenAI low plus" in p.read_text(encoding="utf-8")


def test_append_empty_does_not_create_file(tmp_path: Path):
    p = append_day_leads(tmp_path, "2026-04-17", [])
    assert p is None
    assert not (tmp_path / "2026" / "04" / "17.md").exists()


def test_regenerate_latest_excludes_expired(tmp_path: Path):
    today = date.today()
    fresh = HotLead(
        id="fresh", captured_at=today.isoformat(), title="fresh", summary="",
        category="arbitrage", source_group="G", source_sender="A", status="alive",
    )
    expired = HotLead(
        id="expired",
        captured_at=(today - timedelta(days=20)).isoformat(),
        title="expired", summary="",
        category="arbitrage", source_group="G", source_sender="A", status="alive",
    )
    append_day_leads(tmp_path, fresh.captured_at, [fresh])
    append_day_leads(tmp_path, expired.captured_at, [expired])

    latest = tmp_path / "latest.md"
    regenerate_latest(tmp_path, latest, retention_days=14)
    text = latest.read_text(encoding="utf-8")
    assert "fresh" in text
    assert "expired" not in text


def test_regenerate_latest_excludes_dead(tmp_path: Path):
    today = date.today()
    alive = HotLead(id="a", captured_at=today.isoformat(), title="alive", summary="",
                    category="arbitrage", source_group="G", source_sender="A",
                    status="alive")
    dead = HotLead(id="d", captured_at=today.isoformat(), title="dead", summary="",
                   category="arbitrage", source_group="G", source_sender="A",
                   status="dead")
    append_day_leads(tmp_path, today.isoformat(), [alive, dead])
    regenerate_latest(tmp_path, tmp_path / "latest.md", retention_days=14)
    text = (tmp_path / "latest.md").read_text(encoding="utf-8")
    assert "alive" in text
    assert "dead" not in text
```

- [ ] **Step 2: Run, verify fail**

```bash
pytest tests/test_hot_leads.py -v
```

Expected: FAIL.

- [ ] **Step 3: Implement**

Create `src/chat_daily_tg/hot_leads.py`:

```python
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
import json
from pathlib import Path


@dataclass
class HotLead:
    id: str
    captured_at: str           # YYYY-MM-DD
    title: str
    summary: str
    category: str              # arbitrage | bug | personal_trick | gray_zone
    source_group: str
    source_sender: str
    status: str                # alive | likely_dead | dead
    risk_notes: str | None = None
    death_signal: str | None = None


def _day_file(root: Path, date_str: str) -> Path:
    y, m, d = date_str.split("-")
    return root / y / m / f"{d}.md"


def _day_jsonl(root: Path, date_str: str) -> Path:
    """Internal storage: every day's new leads stored as JSONL alongside md."""
    y, m, d = date_str.split("-")
    return root / y / m / f"{d}.jsonl"


def append_day_leads(root: Path, date_str: str, leads: list[HotLead]) -> Path | None:
    """Write that day's new hot leads to YYYY/MM/DD.md and .jsonl.
    Returns md path if anything was written, else None.
    """
    if not leads:
        return None
    md = _day_file(root, date_str)
    jl = _day_jsonl(root, date_str)
    md.parent.mkdir(parents=True, exist_ok=True)

    md_lines = [f"# {date_str} 热点板新增", ""]
    for lead in leads:
        md_lines.extend([
            f"## {lead.title}",
            f"- 出处：{lead.source_group} / {lead.source_sender}",
            f"- 分类：{lead.category}",
            f"- 摘要：{lead.summary}",
            f"- 状态：{lead.status}",
            f"- ID：`{lead.id}`",
            "",
        ])
        if lead.risk_notes:
            md_lines.insert(-1, f"- 风险：{lead.risk_notes}")
    md.write_text("\n".join(md_lines), encoding="utf-8")

    # JSONL
    with open(jl, "a", encoding="utf-8") as f:
        for lead in leads:
            f.write(json.dumps(asdict(lead), ensure_ascii=False) + "\n")
    return md


def load_all_leads(root: Path) -> list[HotLead]:
    """Walk all YYYY/MM/DD.jsonl and load."""
    out: list[HotLead] = []
    if not root.exists():
        return out
    for jl in sorted(root.rglob("*.jsonl")):
        with open(jl, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(HotLead(**json.loads(line)))
    return out


def regenerate_latest(root: Path, latest_md: Path, retention_days: int = 14) -> None:
    cutoff = date.today() - timedelta(days=retention_days)
    leads = load_all_leads(root)
    active = [
        l for l in leads
        if l.status == "alive"
        and date.fromisoformat(l.captured_at) >= cutoff
    ]
    # Group by category
    by_cat: dict[str, list[HotLead]] = {}
    for l in active:
        by_cat.setdefault(l.category, []).append(l)

    lines = [
        "# 热点板 — 活跃机会",
        f"> 保留窗口：{retention_days} 天；自动生成，改 YYYY/MM/DD.jsonl 不改这里",
        "",
    ]
    for cat, items in sorted(by_cat.items()):
        lines.append(f"## {cat}")
        lines.append("")
        for l in sorted(items, key=lambda x: x.captured_at, reverse=True):
            lines.extend([
                f"### {l.title} ({l.captured_at})",
                f"- 来源：{l.source_group} / {l.source_sender}",
                f"- 摘要：{l.summary}",
            ])
            if l.risk_notes:
                lines.append(f"- 风险：{l.risk_notes}")
            lines.append(f"- ID：`{l.id}`")
            lines.append("")

    latest_md.parent.mkdir(parents=True, exist_ok=True)
    latest_md.write_text("\n".join(lines), encoding="utf-8")


def mark_lead_status(root: Path, lead_id: str, status: str,
                      death_signal: str | None = None) -> bool:
    """Find a lead by id across all day-jsonl files and update its status."""
    if not root.exists():
        return False
    found = False
    for jl in root.rglob("*.jsonl"):
        leads = []
        modified = False
        with open(jl, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                if data.get("id") == lead_id:
                    data["status"] = status
                    if death_signal is not None:
                        data["death_signal"] = death_signal
                    modified = True
                    found = True
                leads.append(data)
        if modified:
            with open(jl, "w", encoding="utf-8") as f:
                for data in leads:
                    f.write(json.dumps(data, ensure_ascii=False) + "\n")
    return found
```

- [ ] **Step 4: Run, verify pass**

```bash
pytest tests/test_hot_leads.py -v
```

Expected: 4 PASSED.

- [ ] **Step 5: Commit**

```bash
git add src/chat_daily_tg/hot_leads.py tests/test_hot_leads.py
git commit -m "feat(hot_leads): day-file + latest aggregation with retention"
```

---

### Task 3.6: Integrate opportunity extraction into run_daily

**Files:**
- Modify: `run_daily.py`

- [ ] **Step 1: Add opportunity persistence after LLM summary**

Append the following logic to `run_daily.py`'s `_run` function, just after the `(archive_dir / "summary.md").write_text(...)` line:

Open `run_daily.py` and insert before "# Push Telegram" block:

```python
    # 4.5. Persist opportunities
    from datetime import datetime as _dt
    from chat_daily_tg.db import PermanentDB, PermanentEntry
    from chat_daily_tg.hot_leads import HotLead, append_day_leads, regenerate_latest
    from chat_daily_tg.permanent_md import regenerate_permanent_md
    from chat_daily_tg.paths import (
        PERMANENT_JSONL, PERMANENT_MD, HOT_LEADS_DIR, HOT_LEADS_LATEST,
    )

    pdb = PermanentDB(PERMANENT_JSONL)
    for i, add in enumerate(out.opportunities.get("permanent_additions", [])):
        entry = PermanentEntry(
            id=f"{date_str}-perm-{i:03d}",
            captured_at=_dt.now().isoformat(),
            source_group=add.get("source_group", ""),
            source_sender=add.get("source_sender", ""),
            category=add.get("category", "misc"),
            type=add.get("type", "permanent"),
            title=add.get("title", ""),
            content=add.get("content", ""),
            url=add.get("url"),
            expires_at=add.get("expires_at"),
            notes=add.get("notes"),
        )
        pdb.append(entry)
        log.info("permanent add: %s", entry.title)

    hot_leads_new: list[HotLead] = []
    for i, add in enumerate(out.opportunities.get("hot_leads_additions", [])):
        lead = HotLead(
            id=f"{date_str}-hot-{i:03d}",
            captured_at=date_str,
            title=add.get("title", ""),
            summary=add.get("summary", ""),
            category=add.get("category", "arbitrage"),
            source_group=add.get("source_group", ""),
            source_sender=add.get("source_sender", ""),
            status="alive",
            risk_notes=add.get("risk_notes"),
        )
        hot_leads_new.append(lead)
    append_day_leads(HOT_LEADS_DIR, date_str, hot_leads_new)
    log.info("hot leads added: %d", len(hot_leads_new))

    # Regenerate derived views
    regenerate_permanent_md(PERMANENT_JSONL, PERMANENT_MD)
    regenerate_latest(HOT_LEADS_DIR, HOT_LEADS_LATEST, retention_days=cfg.hot_leads.retention_days)
```

- [ ] **Step 2: Run all tests to ensure no regression**

```bash
pytest
```

Expected: all prior tests still pass.

- [ ] **Step 3: Smoke-run the pipeline on 2026-04-17**

```bash
python run_daily.py --date 2026-04-17
```

Expected:
- Log shows `permanent add: <title>` entries
- Log shows `hot leads added: N`
- `~/chat-daily/permanent.jsonl` now has lines
- `~/chat-daily/permanent.md` has tables
- `~/chat-daily/hot-leads/2026/04/17.md` exists (if any hot leads)
- `~/chat-daily/hot-leads/latest.md` exists with aggregated view

- [ ] **Step 4: Commit**

```bash
git add run_daily.py
git commit -m "feat: persist opportunities to permanent.jsonl + hot-leads board"
```

---

## Phase 4 — Death signals + context-aware prompt

### Task 4.1: Extend prompt with active-opportunity context

**Files:**
- Modify: `src/chat_daily_tg/prompts.py` (already has context params)
- Create: `src/chat_daily_tg/context_builder.py`
- Create: `tests/test_context_builder.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_context_builder.py`:

```python
from datetime import date, timedelta
from pathlib import Path
from chat_daily_tg.db import PermanentDB, PermanentEntry
from chat_daily_tg.hot_leads import HotLead, append_day_leads
from chat_daily_tg.context_builder import (
    active_permanent_summary, active_hot_leads_summary,
)


def test_active_permanent_summary_lists_alive(tmp_path: Path):
    db = PermanentDB(tmp_path / "p.jsonl")
    db.append(PermanentEntry(
        id="alive1", captured_at="2026-04-17", source_group="G", source_sender="A",
        category="invite_code", type="permanent", title="Alive invite", content="X",
        status="alive",
    ))
    db.append(PermanentEntry(
        id="dead1", captured_at="2026-04-17", source_group="G", source_sender="A",
        category="invite_code", type="permanent", title="Dead invite", content="Y",
        status="dead",
    ))
    s = active_permanent_summary(db.path, max_items=50)
    assert "Alive invite" in s
    assert "Dead invite" not in s
    assert "alive1" in s


def test_active_hot_leads_summary_only_within_window(tmp_path: Path):
    today = date.today()
    append_day_leads(tmp_path, today.isoformat(), [
        HotLead(id="fresh", captured_at=today.isoformat(), title="Fresh lead",
                summary="", category="arbitrage", source_group="G",
                source_sender="A", status="alive"),
    ])
    append_day_leads(tmp_path, (today - timedelta(days=30)).isoformat(), [
        HotLead(id="old", captured_at=(today - timedelta(days=30)).isoformat(),
                title="Old lead", summary="", category="arbitrage",
                source_group="G", source_sender="A", status="alive"),
    ])
    s = active_hot_leads_summary(tmp_path, retention_days=14)
    assert "Fresh lead" in s
    assert "Old lead" not in s
```

- [ ] **Step 2: Run, verify fail**

```bash
pytest tests/test_context_builder.py -v
```

Expected: FAIL.

- [ ] **Step 3: Implement**

Create `src/chat_daily_tg/context_builder.py`:

```python
from __future__ import annotations
from datetime import date, timedelta
from pathlib import Path
from chat_daily_tg.db import PermanentDB
from chat_daily_tg.hot_leads import load_all_leads


def active_permanent_summary(db_path: Path, max_items: int = 50) -> str:
    """Short markdown listing alive permanent entries (id + title + category)."""
    db = PermanentDB(db_path)
    lines = []
    for e in db.read_all():
        if e.status != "alive":
            continue
        lines.append(f"- `{e.id}` [{e.category}] {e.title}")
        if len(lines) >= max_items:
            break
    if not lines:
        return "(空)"
    return "\n".join(lines)


def active_hot_leads_summary(root: Path, retention_days: int = 14,
                              max_items: int = 50) -> str:
    cutoff = date.today() - timedelta(days=retention_days)
    leads = load_all_leads(root)
    lines = []
    for l in leads:
        if l.status != "alive":
            continue
        if date.fromisoformat(l.captured_at) < cutoff:
            continue
        lines.append(f"- `{l.id}` [{l.category}] {l.title} ({l.captured_at})")
        if len(lines) >= max_items:
            break
    if not lines:
        return "(空)"
    return "\n".join(lines)
```

- [ ] **Step 4: Run, verify pass**

```bash
pytest tests/test_context_builder.py -v
```

Expected: 2 PASSED.

- [ ] **Step 5: Wire context into run_daily**

In `run_daily.py`, update the LLM call section. Find:

```python
    out = run_summary(
        llm_client=llm, date=date_str,
        groups_with_content=groups_with_content, detail_path=detail_path,
    )
```

Replace with:

```python
    from chat_daily_tg.context_builder import (
        active_permanent_summary, active_hot_leads_summary,
    )
    from chat_daily_tg.paths import PERMANENT_JSONL, HOT_LEADS_DIR

    perm_ctx = active_permanent_summary(PERMANENT_JSONL)
    hot_ctx = active_hot_leads_summary(
        HOT_LEADS_DIR, retention_days=cfg.hot_leads.retention_days,
    )
    log.info("LLM context: permanent=%d chars, hot_leads=%d chars",
             len(perm_ctx), len(hot_ctx))

    out = run_summary(
        llm_client=llm, date=date_str,
        groups_with_content=groups_with_content, detail_path=detail_path,
        active_permanent_summary=perm_ctx,
        active_hot_leads_summary=hot_ctx,
    )
```

- [ ] **Step 6: Commit**

```bash
git add src/chat_daily_tg/context_builder.py tests/test_context_builder.py run_daily.py
git commit -m "feat(context): pass active-opportunity summaries into LLM prompt"
```

---

### Task 4.2: Apply LLM-returned death signals

**Files:**
- Create: `src/chat_daily_tg/death_signals.py`
- Create: `tests/test_death_signals.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_death_signals.py`:

```python
from pathlib import Path
from chat_daily_tg.db import PermanentDB, PermanentEntry
from chat_daily_tg.hot_leads import HotLead, append_day_leads, load_all_leads
from chat_daily_tg.death_signals import apply_death_signals


def test_apply_high_confidence_marks_dead(tmp_path: Path):
    db = PermanentDB(tmp_path / "p.jsonl")
    db.append(PermanentEntry(
        id="target1", captured_at="2026-04-17", source_group="G", source_sender="A",
        category="invite_code", type="permanent", title="Target", content="X",
    ))
    signals = [
        {"target_title_or_id": "target1", "signal_text": "关门了",
         "signal_source": "Bob in G2 18:00", "confidence": "high"},
    ]
    applied = apply_death_signals(
        signals, db_path=tmp_path / "p.jsonl", hot_leads_root=tmp_path / "hl",
    )
    assert applied == 1
    e = db.find("target1")
    assert e.status == "dead"
    assert e.death_signal == "关门了"


def test_apply_medium_confidence_marks_likely_dead(tmp_path: Path):
    db = PermanentDB(tmp_path / "p.jsonl")
    db.append(PermanentEntry(
        id="target1", captured_at="2026-04-17", source_group="G", source_sender="A",
        category="invite_code", type="permanent", title="Target", content="X",
    ))
    signals = [
        {"target_title_or_id": "target1", "signal_text": "好像不行了",
         "signal_source": "X", "confidence": "medium"},
    ]
    apply_death_signals(signals, db_path=tmp_path / "p.jsonl",
                        hot_leads_root=tmp_path / "hl")
    assert db.find("target1").status == "likely_dead"


def test_apply_low_confidence_ignored(tmp_path: Path):
    db = PermanentDB(tmp_path / "p.jsonl")
    db.append(PermanentEntry(
        id="target1", captured_at="2026-04-17", source_group="G", source_sender="A",
        category="invite_code", type="permanent", title="Target", content="X",
    ))
    apply_death_signals(
        [{"target_title_or_id": "target1", "signal_text": "?",
          "signal_source": "X", "confidence": "low"}],
        db_path=tmp_path / "p.jsonl", hot_leads_root=tmp_path / "hl",
    )
    assert db.find("target1").status == "alive"


def test_target_matched_by_title_fallback(tmp_path: Path):
    db = PermanentDB(tmp_path / "p.jsonl")
    db.append(PermanentEntry(
        id="long-id-abc", captured_at="2026-04-17", source_group="G",
        source_sender="A", category="invite_code", type="permanent",
        title="Chase vx 2x 打法", content="...",
    ))
    applied = apply_death_signals(
        [{"target_title_or_id": "Chase vx 2x 打法", "signal_text": "关门了",
          "signal_source": "X", "confidence": "high"}],
        db_path=tmp_path / "p.jsonl", hot_leads_root=tmp_path / "hl",
    )
    assert applied == 1
    assert db.find("long-id-abc").status == "dead"
```

- [ ] **Step 2: Run, verify fail**

```bash
pytest tests/test_death_signals.py -v
```

Expected: FAIL.

- [ ] **Step 3: Implement**

Create `src/chat_daily_tg/death_signals.py`:

```python
from __future__ import annotations
import logging
from pathlib import Path
from chat_daily_tg.db import PermanentDB
from chat_daily_tg.hot_leads import load_all_leads, mark_lead_status

log = logging.getLogger(__name__)


CONFIDENCE_TO_STATUS = {
    "high": "dead",
    "medium": "likely_dead",
    "low": None,   # ignore
}


def apply_death_signals(
    signals: list[dict], db_path: Path, hot_leads_root: Path,
) -> int:
    """Update permanent DB and hot-leads jsonl based on LLM-returned signals.

    Returns number of entries updated.
    """
    db = PermanentDB(db_path)
    updated = 0
    # Build id→entry and title→id index for permanent DB
    perm_by_id = {e.id: e for e in db.read_all()}
    perm_title_to_id = {e.title: e.id for e in perm_by_id.values()}

    # Build id→lead index for hot-leads
    hot_leads = load_all_leads(hot_leads_root) if hot_leads_root.exists() else []
    hot_by_id = {l.id: l for l in hot_leads}
    hot_title_to_id = {l.title: l.id for l in hot_leads}

    for sig in signals:
        conf = sig.get("confidence", "low").lower()
        status = CONFIDENCE_TO_STATUS.get(conf)
        if status is None:
            log.info("death signal low confidence, ignored: %s", sig)
            continue
        target = sig.get("target_title_or_id", "").strip()
        if not target:
            continue
        signal_text = sig.get("signal_text", "")

        # Try exact id in permanent, then title
        pid = target if target in perm_by_id else perm_title_to_id.get(target)
        if pid:
            if db.mark_status(pid, status=status, death_signal=signal_text):
                updated += 1
                log.info("marked permanent %s → %s (%s)", pid, status, signal_text)
                continue

        # Try hot-leads
        hid = target if target in hot_by_id else hot_title_to_id.get(target)
        if hid:
            if mark_lead_status(hot_leads_root, hid, status=status,
                                death_signal=signal_text):
                updated += 1
                log.info("marked hot_lead %s → %s (%s)", hid, status, signal_text)
                continue

        log.warning("death signal target not found: %s", target)

    return updated
```

- [ ] **Step 4: Run, verify pass**

```bash
pytest tests/test_death_signals.py -v
```

Expected: 4 PASSED.

- [ ] **Step 5: Wire into run_daily**

In `run_daily.py`, after the `append_day_leads(...)` call and before the `regenerate_*` calls, insert:

```python
    from chat_daily_tg.death_signals import apply_death_signals as _apply_ds
    n_updated = _apply_ds(
        signals=out.opportunities.get("death_signals", []),
        db_path=PERMANENT_JSONL,
        hot_leads_root=HOT_LEADS_DIR,
    )
    log.info("death signals applied: %d", n_updated)
```

- [ ] **Step 6: Commit**

```bash
git add src/chat_daily_tg/death_signals.py tests/test_death_signals.py run_daily.py
git commit -m "feat(death_signals): mark permanent + hot-lead entries dead from LLM signals"
```

---

## Phase 5 — Documentation and post-deploy verification

### Task 5.1: Write README with operator runbook

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Expand README with operator docs**

Replace `README.md` with:

```markdown
# chat-daily-tg

Daily WeChat group summary → Telegram bot, powered by local CLIProxyAPI.

## What it does

Every morning at 08:00 (macOS launchd):
1. Exports yesterday's messages from configured WeChat groups via `wx-cli`
2. Summarizes via Claude (through local CLIProxyAPI, using your Claude Code subscription)
3. Pushes concise summary to your Telegram bot
4. Archives detailed summary + raw exports under `~/chat-daily/archive/YYYY/MM/DD/`
5. Extracts long-term opportunities (invite codes, bank products) → `permanent.jsonl`
6. Extracts short-term opportunities (arbitrage, bugs) → `hot-leads/` with 14-day rolloff
7. Scans for death signals ("关门了" / "封了") to auto-mark dead items

## Setup (one-time)

1. Install wx-cli and run `sudo wx init` (see wx-cli docs)
2. Install & configure CLIProxyAPI, start it on `127.0.0.1:8317`
3. Create Telegram bot via @BotFather, obtain token + your chat_id
4. `pip install -e ".[dev]"` in project venv
5. Configure `~/chat-daily/config.yaml` (list target groups)
6. Export these env vars in `~/.zshenv`:
   - `CLIPROXY_API_KEY`
   - `TG_BOT_TOKEN`
   - `TG_CHAT_ID`
7. Run `./scripts/install-launchd.sh` to install the daily schedule

## Run manually

```bash
source venv/bin/activate
python run_daily.py              # yesterday (default)
python run_daily.py --date 2026-04-17
```

## Test

```bash
pytest
```

## Upgrade model

Edit `~/chat-daily/config.yaml` `llm.model` field. No code change. Validate:

```bash
curl -s http://127.0.0.1:8317/v1/models \
  -H "Authorization: Bearer $CLIPROXY_API_KEY" | \
  python3 -c "import json,sys; d=json.load(sys.stdin); print('\n'.join(m['id'] for m in d['data']))"
```

## Troubleshooting

- No TG message: `tail -f ~/chat-daily/logs/*.log` — check for Telegram API errors
- Export empty: WeChat must be running + logged in when launchd fires. If Mac was asleep, launchd fires on wake.
- Model not available: `/v1/models` should list it. Check CLIProxyAPI `config.yaml`.

## Data layout

```
~/chat-daily/
├── config.yaml
├── permanent.jsonl
├── permanent.md
├── hot-leads/
│   ├── latest.md
│   └── 2026/04/17.md
├── archive/
│   └── 2026/04/17/
│       ├── <group>.md
│       └── summary.md
└── logs/
    └── 2026-04-17.log
```

## Design docs

- Spec: `docs/specs/2026-04-18-design.md`
- Plan: `docs/plans/2026-04-18-chat-daily-tg.md`
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: operator runbook README"
```

---

### Task 5.2: End-to-end verification on yesterday

**Files:** none (manual).

- [ ] **Step 1: Clean run on yesterday to verify full pipeline**

```bash
cd /Users/Apple/projects/chat-daily-tg
source venv/bin/activate
python run_daily.py --date 2026-04-17
```

Expected:
- Log shows export, LLM call, permanent/hot-leads counts, death signal count, TG push
- Telegram bot receives concise summary
- Files exist at expected paths

- [ ] **Step 2: Verify launchd agent will run tomorrow**

```bash
launchctl list | grep chat-daily-tg
```

Expected: one line, exit status 0 (means agent loaded OK).

- [ ] **Step 3: Check next fire time**

```bash
launchctl print gui/$(id -u)/com.apple.chat-daily-tg | grep -E "next|state"
```

Expected: shows next scheduled time ~08:00 tomorrow.

- [ ] **Step 4: Review all paths populated**

```bash
tree ~/chat-daily -L 3
```

Expected: `config.yaml`, `permanent.*`, `hot-leads/latest.md`, `archive/2026/04/17/` all present.

- [ ] **Step 5: Commit final state**

```bash
# Nothing to add — this is a verification task.
git log --oneline | head -20
```

Expected: a clean commit history with ~15 commits.

---

## Self-Review Checklist

### 1. Spec coverage

| Spec section | Implementing task(s) |
|---|---|
| §1 Goals — daily dedup + long-term DB | Phase 1 (dedup) + Phase 3 (DB) |
| §2 Non-goals | N/A — not implemented, consistent |
| §3 Architecture — launchd → pipeline | Task 2.4 + Task 1.8 |
| §4.1 permanent.jsonl | Task 3.3 |
| §4.2 permanent.md regen | Task 3.4 |
| §4.3 hot-leads/YYYY/MM/DD.md | Task 3.5 |
| §4.4 hot-leads/latest.md | Task 3.5 |
| §4.5 archive dir | Task 1.3 |
| §5.1 Export | Task 1.2 |
| §5.2 Fingerprint | Task 3.1 |
| §5.3 Cross-group dedup | Task 3.2 |
| §5.4 LLM prompt | Task 1.6 + 1.7 |
| §5.5 Death signals | Task 4.1 + 4.2 |
| §5.6 TG push + split | Task 1.5 |
| §5.7 Retry + macOS notifier | Tasks 2.1 + 2.2 |
| §6 Directory layout | Task 1.1 (`paths.py`) |
| §7.1 config.yaml | Task 1.1 |
| §7.2 launchd plist | Task 2.4 |
| §8 Components table | All Phase 1-4 tasks |
| §9 Error handling matrix | Task 2.1 + Task 2.3 |
| §10 Dependencies | Task 0.0 pyproject.toml |
| §11 Phases | This plan's phase numbering matches |
| §12 Open questions | Out of v1 scope (intentionally) |
| §13 Examples | Validated by Task 1.7 smoke |

### 2. Placeholder scan

- No "TBD" / "fill in later" / "implement appropriately" phrases — each step shows exact code.
- Environment values (API keys, tokens, chat_ids) use env var references, with setup steps in Phase 0.

### 3. Type consistency

- `PermanentEntry` fields match `db.py`, `permanent_md.py`, `death_signals.py`, `context_builder.py` usage ✓
- `HotLead` fields match `hot_leads.py`, `death_signals.py`, `context_builder.py` usage ✓
- `LLMClient.chat()` signature `(prompt, system=None)` used consistently ✓
- `TelegramSender.send(text, parse_mode=None)` signature consistent ✓
- `ExportResult.message_count` / `out_path` accessed consistently ✓
- `SummaryOutput.concise_md` / `detailed_md` / `opportunities` used consistently in Tasks 1.7, 1.8, 3.6, 4.2 ✓

### 4. Ambiguity

- "Yesterday" is always `date.today() - timedelta(days=1)` per local TZ (macOS default). Spec §7.1 specifies `timezone: Asia/Shanghai` which matches macOS default for the user — no conversion code needed for v1.
- Invite code regex uses contextual requirement (must follow "邀请码/推荐码/invite code" keyword) to avoid false positives. A bare 5-12 char alnum string does not match by itself.
