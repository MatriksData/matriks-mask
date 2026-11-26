"""Uçtan uca: gerçek LiteLLM proxy + OpenAI uyumlu echo sunucusu (`pytest -m e2e`).

Doğrulananlar: sağlayıcıya (echo) yalnız yer tutucular gider; istemci orijinal değerleri alır (stream + non-stream);
StandardLoggingPayload, audit JSONL ve proxy logu ham değer içermez; BLOCK 400; fail-closed'da echo istek almaz.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.e2e
HERE = Path(__file__).resolve().parent
ECHO_PORT, PROXY_PORT = 9911, 4010
AUTH = {"Authorization": "Bearer sk-test"}
T1 = "17291716060"
IBAN1 = "TR330006100519786457841326"
PII_TEXT = f"Ben Ayşe Demir, TCKN {T1}, tel 0532 234 56 78, e-posta ayse@ornek.com, IBAN {IBAN1}."
SECRETS = ["Ayşe Demir", T1, "0532 234 56 78", "ayse@ornek.com", IBAN1]


def _free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def _wait(url: str, timeout: float = 240) -> None:  # yüklü makinede LiteLLM proxy açılışı 2 dakikayı aşabiliyor
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"hazır değil: {url}")


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    if not (_free(ECHO_PORT) and _free(PROXY_PORT)):
        pytest.skip("e2e portları dolu")
    work = tmp_path_factory.mktemp("e2e")
    audit = work / "audit.jsonl"
    leak = work / "leak.jsonl"
    cfg = (HERE / "config.yaml").read_text(encoding="utf-8").replace("os.environ/MATRIKS_PII_AUDIT_PATH", audit.as_posix())
    (work / "config.yaml").write_text(cfg, encoding="utf-8")
    shutil.copy(HERE / "custom_callbacks.py", work / "custom_callbacks.py")
    env = {**os.environ, "PYTHONUTF8": "1", "LEAK_LOG_PATH": str(leak), "LITELLM_LOG": "INFO"}
    proxy_log = open(work / "proxy.log", "w", encoding="utf-8")  # noqa: SIM115
    echo = subprocess.Popen([sys.executable, str(HERE / "echo_server.py"), str(ECHO_PORT)], env=env)
    proxy = subprocess.Popen(
        [sys.executable, "-m", "litellm.proxy.proxy_cli", "--config", str(work / "config.yaml"), "--port", str(PROXY_PORT)],
        cwd=work, env=env, stdout=proxy_log, stderr=subprocess.STDOUT,
    )
    try:
        _wait(f"http://127.0.0.1:{ECHO_PORT}/v1/models")
        _wait(f"http://127.0.0.1:{PROXY_PORT}/health/liveliness")
        yield {"audit": audit, "leak": leak, "proxy_log": work / "proxy.log", "work": work}
    finally:
        proxy.terminate()
        echo.terminate()
        proxy.wait(30)
        echo.wait(30)
        proxy_log.close()


def _chat(content: str, stream: bool = False, chunk: int = 5, **extra: Any) -> httpx.Response:
    body = {"model": "echo", "messages": [{"role": "user", "content": content}], "stream": stream, **extra}
    return httpx.post(f"http://127.0.0.1:{PROXY_PORT}/v1/chat/completions", json=body,
                      headers={**AUTH, "X-Echo-Chunk-Size": str(chunk)}, timeout=60)


def _received() -> list[dict[str, Any]]:
    return httpx.get(f"http://127.0.0.1:{ECHO_PORT}/_received").json()


def _reset() -> None:
    httpx.post(f"http://127.0.0.1:{ECHO_PORT}/_reset")


def _sse_text(r: httpx.Response) -> str:
    out = ""
    for line in r.text.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            d = json.loads(line[6:])
            for ch in d.get("choices", []):
                out += (ch.get("delta") or {}).get("content") or ""
    return out


def _no_secret(blob: str) -> None:
    for s in SECRETS:
        assert s not in blob, f"sızıntı: {s}"


def test_roundtrip_non_stream(stack: dict[str, Any]) -> None:
    _reset()
    r = _chat(PII_TEXT)
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"] == PII_TEXT
    sent = json.dumps(_received(), ensure_ascii=False)
    _no_secret(sent)
    assert "<KISI_1>" in sent and "<TCKN_1>" in sent and "<IBAN_1>" in sent
    assert "matriks-pii" in r.headers.get("x-litellm-applied-guardrails", "")


@pytest.mark.parametrize("chunk", [1, 3, 5])
def test_roundtrip_stream(stack: dict[str, Any], chunk: int) -> None:
    _reset()
    r = _chat(PII_TEXT, stream=True, chunk=chunk)
    assert r.status_code == 200
    assert _sse_text(r) == PII_TEXT
    _no_secret(json.dumps(_received(), ensure_ascii=False))


def test_tool_call_roundtrip(stack: dict[str, Any]) -> None:
    r = _chat(f"TOOL:{T1} numaralı kişi")
    args = json.loads(r.json()["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])
    assert args == {"text": f"{T1} numaralı kişi"}


def test_suffix_repair_e2e(stack: dict[str, Any]) -> None:
    r = _chat("Ahmet Yılmaz'a ilet")
    assert r.json()["choices"][0]["message"]["content"] == "Ahmet Yılmaz'a ilet"


def test_block_400(stack: dict[str, Any]) -> None:
    _reset()
    r = _chat("Şifre: Gizli123! lütfen kaydet")
    assert r.status_code == 400
    assert "Gizli123" not in r.text and "matriks_pii_blocked" in r.text
    assert _received() == []


def test_opt_in_guardrail_only_when_requested(stack: dict[str, Any]) -> None:
    r = _chat(f"TCKN {T1}", guardrails=["matriks-pii-optin"])
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == f"TCKN {T1}"


def test_parallel_sessions_do_not_mix(stack: dict[str, Any]) -> None:
    import concurrent.futures as cf

    vals = ["17291716060", "44771278268", "26197306992", "52393849902"]
    with cf.ThreadPoolExecutor(16) as ex:
        res = list(ex.map(lambda i: _chat(f"TCKN {vals[i % 4]}").json()["choices"][0]["message"]["content"], range(100)))
    assert res == [f"TCKN {vals[i % 4]}" for i in range(100)]


def test_logs_value_free(stack: dict[str, Any]) -> None:
    _chat(PII_TEXT)
    time.sleep(3)  # loglama arka planda
    leak = stack["leak"].read_text(encoding="utf-8") if stack["leak"].exists() else ""
    assert leak, "leak logger çalışmadı"
    for line in leak.splitlines():
        rec = json.loads(line)
        _no_secret(json.dumps(rec["standard_logging_object"], ensure_ascii=False, default=str))
        _no_secret(json.dumps(rec["messages"], ensure_ascii=False, default=str))
    audit = "".join(p.read_text(encoding="utf-8") for p in stack["work"].glob("audit*.jsonl"))
    assert audit and '"TCKN"' in audit
    _no_secret(audit)


def test_proxy_log_value_free(stack: dict[str, Any]) -> None:
    _no_secret(stack["proxy_log"].read_text(encoding="utf-8", errors="replace"))


def test_special_category_log_count_in_guardrail_information(stack: dict[str, Any]) -> None:
    before = len(stack["leak"].read_text(encoding="utf-8").splitlines()) if stack["leak"].exists() else 0
    r = _chat("Müşterimiz Ayşe Demir CHP üyesi.")
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "Müşterimiz Ayşe Demir CHP üyesi."
    time.sleep(3)
    recs = [json.loads(ln) for ln in stack["leak"].read_text(encoding="utf-8").splitlines()[before:]]
    infos = [i for rec in recs for i in (rec["standard_logging_object"] or {}).get("guardrail_information") or []]
    pre = [i for i in infos if (i.get("guardrail_response") or {}).get("logged")]
    assert pre and pre[0]["guardrail_response"]["logged"] == {"OZEL_SIYASI": 1}
    _no_secret(json.dumps(infos, ensure_ascii=False, default=str))


def test_fail_closed_provider_gets_nothing(stack: dict[str, Any]) -> None:
    _reset()
    r = _chat(PII_TEXT + " " + "x" * 1100, guardrails=["matriks-pii-broken"])  # max_text_chars: 1024 aşılır
    assert r.status_code in (413, 500) and "matriks_pii_" in r.text
    _no_secret(r.text)
    sent = json.dumps(_received(), ensure_ascii=False)
    assert T1 not in sent and IBAN1 not in sent  # sağlayıcıya ham değer gitmedi



# ---------------------------------------------------------------- Gemini native (generateContent)
def _gemini(stream: bool = False) -> httpx.Response:
    path = "streamGenerateContent?alt=sse" if stream else "generateContent"
    body = {"contents": [{"role": "user", "parts": [{"text": PII_TEXT}]}]}
    return httpx.post(f"http://127.0.0.1:{PROXY_PORT}/v1beta/models/echo-gemini:{path}", json=body, headers=AUTH, timeout=60)


def _gemini_sse_text(r: httpx.Response) -> str:
    out = ""
    for line in r.text.splitlines():
        if line.startswith("data:"):
            d = json.loads(line[5:])
            for c in d.get("candidates", []):
                for p in c.get("content", {}).get("parts", []):
                    out += p.get("text", "")
    return out


def test_gemini_native_roundtrip_non_stream(stack: dict[str, Any]) -> None:
    _reset()
    r = _gemini()
    assert r.status_code == 200, r.text
    assert r.json()["candidates"][0]["content"]["parts"][0]["text"] == PII_TEXT


def test_gemini_native_roundtrip_stream(stack: dict[str, Any]) -> None:
    _reset()
    r = _gemini(stream=True)
    assert r.status_code == 200, r.text
    assert _gemini_sse_text(r) == PII_TEXT
    assert "KISI_1" not in r.text


def test_gemini_native_provider_gets_placeholders(stack: dict[str, Any]) -> None:
    _reset()
    _gemini()
    sent = json.dumps(_received(), ensure_ascii=False)
    _no_secret(sent)
    assert "<KISI_1>" in sent and "<TCKN_1>" in sent


# ---------------------------------------------------------------- 0.3.0 eklemeleri
SECRET_KEY = "sk-abc123def456ghi789jkl012mno345pqr"


def test_secret_masked_and_not_restored(stack: dict[str, Any]) -> None:
    _reset()
    r = _chat(f"api_key={SECRET_KEY} ile bağlan")
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"] == "api_key=**** ile bağlan"  # GIZLI_ANAHTAR geri yüklenmez
    sent = json.dumps(_received(), ensure_ascii=False)
    assert SECRET_KEY not in sent and "<GIZLI_ANAHTAR_1>" in sent
    assert SECRET_KEY not in r.text


@pytest.mark.parametrize("stream", [False, True])
def test_mixed_case_name_roundtrip(stack: dict[str, Any], stream: bool) -> None:
    _reset()
    text = "Saygılarımla, Ahmet YILMAZ'ın talebi ve YILMAZ Ahmet imzası"
    r = _chat(text, stream=stream, chunk=2)
    assert r.status_code == 200
    got = _sse_text(r) if stream else r.json()["choices"][0]["message"]["content"]
    assert got == text
    sent = json.dumps(_received(), ensure_ascii=False)
    assert "YILMAZ" not in sent and "<KISI_1>" in sent


@pytest.mark.parametrize("stream", [False, True])
def test_text_completions_roundtrip(stack: dict[str, Any], stream: bool) -> None:
    _reset()
    r = httpx.post(f"http://127.0.0.1:{PROXY_PORT}/v1/completions", json={"model": "echo", "prompt": PII_TEXT, "stream": stream},
                   headers={**AUTH, "X-Echo-Chunk-Size": "3"}, timeout=60)
    assert r.status_code == 200, r.text
    if stream:
        got = ""
        for ln in r.text.splitlines():
            if ln.startswith("data: ") and ln != "data: [DONE]":
                got += "".join(c.get("text") or "" for c in json.loads(ln[6:])["choices"])
    else:
        got = r.json()["choices"][0]["text"]
    assert got == PII_TEXT
    assert "KISI_1" not in r.text
    _no_secret(json.dumps(_received(), ensure_ascii=False))


def test_guardrail_also_on_model_does_not_break_revert(stack: dict[str, Any]) -> None:
    """Guardrail hem default_on hem modelin `guardrails` listesindeyse yanıt yine geri çevrilir (eskiden 500)."""
    _reset()
    body = {"model": "echo-model-guardrail", "messages": [{"role": "user", "content": PII_TEXT}]}
    r = httpx.post(f"http://127.0.0.1:{PROXY_PORT}/v1/chat/completions", json=body, headers=AUTH, timeout=60)
    assert r.status_code == 200, r.text
    content = r.json()["choices"][0]["message"]["content"]
    assert "Ayşe Demir" in content and T1 in content
    sent = json.dumps(_received()[-1], ensure_ascii=False)
    for secret in SECRETS:
        assert secret not in sent
