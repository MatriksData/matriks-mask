"""Uçtan uca: LiteLLM yanıt önbelleği açıkken bir kullanıcının değerleri başka kullanıcıya dönmemeli.

Önbellek anahtarı maskeli istekten üretilir: "Ben <KISI_1>, TCKN <TCKN_1>." iki farklı kişi için aynıdır. Önbellekte
yer tutuculu yanıt durmalı; guardrail model düzeyinde de tanımlıysa (geri çevirme önbellekten önce) yanıt yazılmamalı.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from test_proxy_e2e import AUTH, HERE, _free, _wait

pytestmark = pytest.mark.e2e
ECHO_PORT, PROXY_PORT = 9915, 4014
A = "Ben Ayşe Demir, TCKN 17291716060."
B = "Ben Mehmet Kaya, TCKN 44771278268."


@pytest.fixture(scope="module")
def cache_stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    if not (_free(ECHO_PORT) and _free(PROXY_PORT)):
        pytest.skip("e2e portları dolu")
    work = tmp_path_factory.mktemp("e2e_cache")
    cfg = (HERE / "config.yaml").read_text(encoding="utf-8")
    cfg = cfg.replace("127.0.0.1:9911", f"127.0.0.1:{ECHO_PORT}").replace("os.environ/MATRIKS_PII_AUDIT_PATH", (work / "a.jsonl").as_posix())
    cfg = cfg.replace("litellm_settings:\n", "litellm_settings:\n  cache: true\n  cache_params:\n    type: local\n")
    assert "cache: true" in cfg and "echo-model-guardrail" in cfg
    (work / "config.yaml").write_text(cfg, encoding="utf-8")
    (work / "custom_callbacks.py").write_text((HERE / "custom_callbacks.py").read_text(encoding="utf-8"), encoding="utf-8")
    env = {**os.environ, "PYTHONUTF8": "1", "LEAK_LOG_PATH": str(work / "leak.jsonl")}
    log = open(work / "proxy.log", "w", encoding="utf-8")  # noqa: SIM115
    echo = subprocess.Popen([sys.executable, str(HERE / "echo_server.py"), str(ECHO_PORT)], env=env)
    proxy = subprocess.Popen(
        [sys.executable, "-m", "litellm.proxy.proxy_cli", "--config", str(work / "config.yaml"), "--port", str(PROXY_PORT)],
        cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    try:
        _wait(f"http://127.0.0.1:{ECHO_PORT}/v1/models")
        _wait(f"http://127.0.0.1:{PROXY_PORT}/health/liveliness")
        yield work
    finally:
        proxy.terminate()
        echo.terminate()
        proxy.wait(30)
        echo.wait(30)
        log.close()


def _chat(model: str, content: str) -> tuple[int, str]:
    r = httpx.post(f"http://127.0.0.1:{PROXY_PORT}/v1/chat/completions", headers=AUTH, timeout=60,
                   json={"model": model, "messages": [{"role": "user", "content": content}]})
    return r.status_code, (r.json()["choices"][0]["message"]["content"] if r.status_code == 200 else r.text)


def _echo_calls() -> int:
    return len(httpx.get(f"http://127.0.0.1:{ECHO_PORT}/_received").json())


@pytest.mark.parametrize("model", ["echo", "echo-model-guardrail"])
def test_cache_never_returns_another_users_values(cache_stack: Path, model: str) -> None:
    assert _chat(model, A) == (200, A)
    assert _chat(model, B) == (200, B)  # aynı maskeli şablon: Ayşe'nin değerleri dönmemeli
    assert _chat(model, A) == (200, A)
    assert _chat(model, B) == (200, B)


def test_cache_still_used_with_default_on_only(cache_stack: Path) -> None:
    """Yalnız default_on tanımda önbellek çalışmaya devam eder (yer tutuculu yanıt saklanır)."""
    text = "Sayın Zeynep Arslan, TCKN 26197306992 kaydınız alındı."
    assert _chat("echo", text) == (200, text)
    n = _echo_calls()
    assert _chat("echo", text) == (200, text)
    assert _echo_calls() == n  # ikinci istek sağlayıcıya gitmedi
