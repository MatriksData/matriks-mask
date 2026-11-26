"""LiteLLM kancaları — gerçek litellm tipleriyle, proxy'siz."""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

import pytest
from fastapi import HTTPException
from helpers import agen, model_response, stream_chunks

from matriks_pii.guardrail import SESSION_KEY, TurkishPIIGuardrail

T1, T2 = "17291716060", "44771278268"
IBAN1 = "TR330006100519786457841326"
SECRETS = [T1, T2, IBAN1, "Ayşe Demir", "Ahmet Yılmaz", "0532 234 56 78", "ayse@ornek.com"]


def G(**kw: Any) -> TurkishPIIGuardrail:
    kw.setdefault("default_on", True)
    kw.setdefault("event_hook", ["pre_call", "post_call"])
    return TurkishPIIGuardrail(guardrail_name="matriks-pii", **kw)


def chat(content: Any, **extra: Any) -> dict[str, Any]:
    d = {"model": "gpt-x", "messages": [{"role": "user", "content": content}], "metadata": {}}
    d.update(extra)
    return d


def no_secret(obj: Any) -> None:
    dumped = json.dumps(obj, ensure_ascii=False, default=str)
    for s in SECRETS:
        assert s not in dumped, f"sızıntı: {s}"


async def pre(g: TurkishPIIGuardrail, data: dict[str, Any], call_type: str = "acompletion") -> Any:
    return await g.async_pre_call_hook(user_api_key_dict=None, cache=None, data=data, call_type=call_type)


# ---------------------------------------------------------------- should_run / call_type
async def test_default_on_masks() -> None:
    g = G()
    d = chat(f"Ben Ayşe Demir, TCKN {T1}.")
    out = await pre(g, d)
    assert out["messages"][0]["content"] == "Ben <KISI_1>, TCKN <TCKN_1>."
    assert g._session_id(d).startswith("matriks-pii:")


async def test_not_default_on_requires_request() -> None:
    g = G(default_on=False)
    d = chat(f"TCKN {T1}")
    assert await pre(g, d) is None and T1 in d["messages"][0]["content"]
    d2 = chat(f"TCKN {T1}", guardrails=["matriks-pii"])
    d2["metadata"]["guardrails"] = ["matriks-pii"]
    out = await pre(g, d2)
    assert out["messages"][0]["content"] == "TCKN <TCKN_1>"


async def test_unsupported_call_type() -> None:
    d = {"model": "dall-e", "prompt": f"TCKN {T1} için afiş", "metadata": {}}
    out = await pre(G(), d, "aimage_generation")
    assert out["prompt"] == "TCKN <TCKN_1> için afiş"  # varsayılan: mask (geri çevirme yok)
    d2 = {"model": "rerank", "query": f"TCKN {T1}", "documents": [f"IBAN {IBAN1}"], "metadata": {}}
    out2 = await pre(G(), d2, "arerank")
    assert T1 not in json.dumps(out2) and IBAN1 not in json.dumps(out2)
    d3 = {"model": "dall-e", "prompt": f"TCKN {T1}", "metadata": {}}
    assert await pre(G(unsupported_call_types="pass"), d3, "aimage_generation") is None
    with pytest.raises(HTTPException):
        await pre(G(unsupported_call_types="block"), {"model": "x", "prompt": f"TCKN {T1}"}, "aimage_generation")


def test_post_call_added_to_event_hook() -> None:
    g = TurkishPIIGuardrail(guardrail_name="x", event_hook="pre_call", default_on=True)
    assert [h.value for h in g.event_hook] == ["pre_call", "post_call", "logging_only"]


def test_native_lifecycle_flag() -> None:
    assert TurkishPIIGuardrail.use_native_lifecycle_hooks is True


# ---------------------------------------------------------------- istek gezgini
async def test_content_parts() -> None:
    g = G()
    d = chat([{"type": "text", "text": f"TCKN {T1}"}, {"type": "image_url", "image_url": {"url": "http://x/17291716060.png"}}])
    out = await pre(g, d)
    parts = out["messages"][0]["content"]
    assert parts[0]["text"] == "TCKN <TCKN_1>" and parts[1]["image_url"]["url"].endswith("17291716060.png")


async def test_tool_call_arguments_json_leaves() -> None:
    g = G()
    d = {"model": "m", "metadata": {}, "messages": [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "1", "type": "function", "function": {"name": "f", "arguments": json.dumps({"tckn": T1, "not": 'tırnak "x"'}, ensure_ascii=False)}}]}]}
    out = await pre(g, d)
    args = json.loads(out["messages"][0]["tool_calls"][0]["function"]["arguments"])
    assert args == {"tckn": "<TCKN_1>", "not": 'tırnak "x"'}


async def test_tool_call_arguments_broken_json() -> None:
    g = G()
    d = {"model": "m", "metadata": {}, "messages": [
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "type": "function", "function": {"name": "f", "arguments": f'{{"tckn": "{T1}"'}}]}]}
    out = await pre(g, d)
    assert out["messages"][0]["tool_calls"][0]["function"]["arguments"] == '{"tckn": "<TCKN_1>"'


async def test_unchanged_json_untouched_bytes() -> None:
    g = G()
    raw = '{ "a" :  1 }'
    d = {"model": "m", "metadata": {}, "messages": [{"role": "assistant", "content": None, "tool_calls": [{"id": "1", "type": "function", "function": {"name": "f", "arguments": raw}}]}]}
    out = await pre(g, d)
    assert out["messages"][0]["tool_calls"][0]["function"]["arguments"] == raw


@pytest.mark.parametrize("flag,role", [("mask_system_messages", "system"), ("mask_tool_messages", "tool"), ("mask_assistant_messages", "assistant")])
async def test_role_filters(flag: str, role: str) -> None:
    d = {"model": "m", "metadata": {}, "messages": [{"role": role, "content": f"TCKN {T1}"}]}
    out = await pre(G(), copy.deepcopy(d))
    assert out["messages"][0]["content"] == "TCKN <TCKN_1>"
    out2 = await pre(G(**{flag: False}), copy.deepcopy(d))
    assert out2["messages"][0]["content"] == f"TCKN {T1}"


async def test_name_field() -> None:
    d = {"model": "m", "metadata": {}, "messages": [{"role": "user", "name": "Ahmet Yılmaz", "content": "merhaba"}]}
    out = await pre(G(), d)
    assert out["messages"][0]["name"] == "<KISI_1>"


async def test_same_value_three_messages_one_placeholder() -> None:
    d = {"model": "m", "metadata": {}, "messages": [{"role": "user", "content": f"A {T1}"}, {"role": "assistant", "content": f"B {T1}"}, {"role": "user", "content": f"C {T1}"}]}
    out = await pre(G(), d)
    assert [m["content"] for m in out["messages"]] == ["A <TCKN_1>", "B <TCKN_1>", "C <TCKN_1>"]


async def test_reserve_existing_literal_placeholder() -> None:
    d = chat("Şablon <KISI_1> kalsın; gerçek ad Ahmet Yılmaz.")
    out = await pre(G(), d)
    assert out["messages"][0]["content"] == "Şablon <KISI_1> kalsın; gerçek ad <KISI_2>."


async def test_responses_api_bucket_and_input() -> None:
    d = {"model": "m", "input": f"TCKN {T1}", "instructions": "Ahmet Yılmaz için yaz", "metadata": {"user": "x"}}
    out = await pre(G(), d, "aresponses")
    assert out["input"] == "TCKN <TCKN_1>" and out["instructions"] == "<KISI_1> için yaz"
    assert SESSION_KEY in out["litellm_metadata"] and SESSION_KEY not in out["metadata"]


async def test_responses_input_items() -> None:
    d = {"model": "m", "litellm_metadata": {}, "input": [
        {"role": "user", "content": [{"type": "input_text", "text": f"TCKN {T1}"}]},
        {"type": "function_call", "call_id": "c", "name": "f", "arguments": json.dumps({"t": T2})},
        {"type": "function_call_output", "call_id": "c", "output": f"sonuç {T2}"}]}
    out = await pre(G(), d, "aresponses")
    assert out["input"][0]["content"][0]["text"] == "TCKN <TCKN_1>"
    assert json.loads(out["input"][1]["arguments"]) == {"t": "<TCKN_2>"}
    assert out["input"][2]["output"] == "sonuç <TCKN_2>"


async def test_embeddings() -> None:
    d = {"model": "e", "metadata": {}, "input": [f"TCKN {T1}", "düz"]}
    out = await pre(G(), d, "aembedding")
    assert out["input"] == ["TCKN <TCKN_1>", "düz"]
    d2 = {"model": "e", "metadata": {}, "input": [[1, 2, 3], [4]]}
    assert (await pre(G(), d2, "aembedding"))["input"] == [[1, 2, 3], [4]]
    d3 = {"model": "e", "metadata": {}, "input": [f"TCKN {T1}"]}
    assert (await pre(G(mask_embeddings=False), d3, "aembedding"))["input"] == [f"TCKN {T1}"]


async def test_anthropic_messages() -> None:
    d = {"model": "claude", "system": [{"type": "text", "text": "Müşteri Ahmet Yılmaz"}], "messages": [
        {"role": "user", "content": [{"type": "text", "text": f"TCKN {T1}"},
                                     {"type": "tool_result", "tool_use_id": "t", "content": [{"type": "text", "text": f"IBAN {IBAN1}"}]}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "f", "input": {"tckn": T1}}]}]}
    out = await pre(G(), d, "anthropic_messages")
    assert out["system"][0]["text"] == "Müşteri <KISI_1>"
    assert out["messages"][0]["content"][0]["text"] == "TCKN <TCKN_1>"
    assert out["messages"][0]["content"][1]["content"][0]["text"] == "IBAN <IBAN_1>"
    assert out["messages"][1]["content"][0]["input"] == {"tckn": "<TCKN_1>"}
    assert SESSION_KEY in out["litellm_metadata"]


async def test_text_completion_prompt() -> None:
    d = {"model": "m", "metadata": {}, "prompt": [f"TCKN {T1}"]}
    assert (await pre(G(), d, "atext_completion"))["prompt"] == ["TCKN <TCKN_1>"]


async def test_proxy_server_request_body_masked() -> None:
    d = chat(f"TCKN {T1}")
    d["proxy_server_request"] = {"body": {"messages": [{"role": "user", "content": f"TCKN {T1}"}]}}
    d["metadata"]["proxy_server_request"] = {"body": {"messages": [{"role": "user", "content": f"TCKN {T1}"}]}}
    out = await pre(G(), d)
    assert out["proxy_server_request"]["body"]["messages"][0]["content"] == "TCKN <TCKN_1>"
    assert out["metadata"]["proxy_server_request"]["body"]["messages"][0]["content"] == "TCKN <TCKN_1>"


# ---------------------------------------------------------------- BLOCK / fail mode / sızıntı
async def test_block_raises_400_without_value_and_request_masked() -> None:
    g = G(entities={"SIFRE": "BLOCK"})
    d = chat(f"Şifre: Gizli123! ve TCKN {T1}")
    with pytest.raises(HTTPException) as ei:
        await pre(g, d)
    assert ei.value.status_code == 400 and ei.value.detail["entity"] == "SIFRE"
    assert "Gizli123" not in json.dumps(ei.value.detail)
    assert d["messages"][0]["content"] == "Şifre: <SIFRE_1> ve TCKN <TCKN_1>"  # loglanacak istek de maskeli


async def test_fail_closed_and_open(monkeypatch: pytest.MonkeyPatch) -> None:
    g = G()

    def boom(_t: str) -> Any:
        raise RuntimeError(f"içerik {T1}")

    monkeypatch.setattr(g.engine, "analyze", boom)
    with pytest.raises(HTTPException) as ei:
        await pre(g, chat(f"TCKN {T1}"))
    assert ei.value.status_code == 500 and T1 not in json.dumps(ei.value.detail)
    g2 = G(fail_mode="open")
    monkeypatch.setattr(g2.engine, "analyze", boom)
    assert await pre(g2, chat(f"TCKN {T1}")) is None


def test_store_map_in_metadata_requires_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MATRIKS_PII_ALLOW_DEBUG_MAP", raising=False)
    with pytest.raises(ValueError):
        G(store_map_in_metadata=True)


async def test_no_values_in_request_data_or_guardrail_info() -> None:
    g = G()
    d = chat(f"Ben Ayşe Demir, TCKN {T1}, tel 0532 234 56 78, ayse@ornek.com, IBAN {IBAN1}")
    out = await pre(g, d)
    no_secret(out)
    info = out["metadata"].get("standard_logging_guardrail_information")
    assert info, "guardrail bilgisi loglanmalı"
    no_secret(info)
    assert info[0]["masked_entity_count"]["TCKN"] == 1


async def test_log_counts_for_special_categories() -> None:
    d = chat("Müşterimiz Ahmet Yılmaz CHP üyesi.")
    out = await pre(G(), d)
    info = out["metadata"]["standard_logging_guardrail_information"][0]
    assert info["guardrail_response"]["logged"] == {"OZEL_SIYASI": 1}
    assert "CHP" in out["messages"][0]["content"]


# ---------------------------------------------------------------- post-call
async def _roundtrip(g: TurkishPIIGuardrail, text: str, llm: Any, **kw: Any) -> tuple[dict, Any, Any]:
    d = chat(text, **kw)
    await pre(g, d)
    resp = llm(d) if callable(llm) else llm
    out = await g.async_post_call_success_hook(data=d, user_api_key_dict=None, response=resp)
    return d, resp, out


async def test_post_call_reverts_on_copy() -> None:
    g = G()
    resp = model_response("Merhaba <KISI_1>'e, TCKN <TCKN_1>.")
    d, orig, out = await _roundtrip(g, f"Ben Ayşe Demir, TCKN {T1}.", resp)
    assert out.choices[0].message.content == f"Merhaba Ayşe Demir'e, TCKN {T1}."
    assert orig.choices[0].message.content == "Merhaba <KISI_1>'e, TCKN <TCKN_1>."
    assert await g.store.get(g._session_id(d)) is None  # oturum silindi


async def test_post_call_tool_calls_reasoning_n2() -> None:
    g = G()
    resp = model_response(["A <TCKN_1>", "B <TCKN_1>"], tool_args=[json.dumps({"t": "<TCKN_1>"})], reasoning="R <TCKN_1>", n=2)
    _, _, out = await _roundtrip(g, f"TCKN {T1}", resp)
    assert out.choices[0].message.content == f"A {T1}" and out.choices[1].message.content == f"B {T1}"
    assert json.loads(out.choices[0].message.tool_calls[0].function.arguments) == {"t": T1}
    assert out.choices[0].message.reasoning_content == f"R {T1}"


async def test_tool_restore_policy_none() -> None:
    g = G(tool_restore_policy="none")
    resp = model_response("A <TCKN_1>", tool_args=[json.dumps({"t": "<TCKN_1>"})])
    _, _, out = await _roundtrip(g, f"TCKN {T1}", resp)
    assert json.loads(out.choices[0].message.tool_calls[0].function.arguments) == {"t": "<TCKN_1>"}


async def test_post_call_list_content_and_dict_responses() -> None:
    g = G()
    d = chat(f"TCKN {T1}")
    await pre(g, d)
    resp = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "x <TCKN_1>"}]},
                       {"type": "function_call", "arguments": json.dumps({"a": "<TCKN_1>"})}]}
    out = await g.async_post_call_success_hook(data=d, user_api_key_dict=None, response=resp)
    assert out["output"][0]["content"][0]["text"] == f"x {T1}"
    assert json.loads(out["output"][1]["arguments"]) == {"a": T1}
    assert resp["output"][0]["content"][0]["text"] == "x <TCKN_1>"


async def test_anthropic_dict_response() -> None:
    g = G()
    d = {"model": "c", "messages": [{"role": "user", "content": f"TCKN {T1}"}]}
    await pre(g, d, "anthropic_messages")
    resp = {"content": [{"type": "text", "text": "t <TCKN_1>"}, {"type": "tool_use", "input": {"x": "<TCKN_1>"}}]}
    out = await g.async_post_call_success_hook(data=d, user_api_key_dict=None, response=resp)
    assert out["content"][0]["text"] == f"t {T1}" and out["content"][1]["input"] == {"x": T1}


async def test_post_call_repeat_is_noop() -> None:
    """Guardrail hem default_on hem model düzeyinde tanımlıysa LiteLLM post_call'u iki kez çağırır."""
    g = G()
    resp = model_response("Merhaba <KISI_1>, TCKN <TCKN_1>.")
    d, _, first = await _roundtrip(g, f"Ben Ayşe Demir, TCKN {T1}.", resp)
    assert first.choices[0].message.content == f"Merhaba Ayşe Demir, TCKN {T1}."
    second = await g.async_post_call_success_hook(data=d, user_api_key_dict=None, response=first)
    assert second is None  # yanıt zaten geri çevrildi: dokunma, hata verme


async def test_post_call_repeat_with_placeholders_still_fails_closed() -> None:
    """Tekrar çağrıya geri çevrilmemiş yanıt gelirse yer tutucu istemciye gitmemeli."""
    g = G()
    resp = model_response("Merhaba <KISI_1>.")
    d, orig, _ = await _roundtrip(g, "Ben Ayşe Demir.", resp)
    with pytest.raises(HTTPException) as e:
        await g.async_post_call_success_hook(data=d, user_api_key_dict=None, response=orig)
    assert e.value.detail["error"] == "matriks_pii_session_missing"


async def test_post_call_repeat_escaped_placeholder_fails_closed() -> None:
    """Tekrar çağrıda markdown kaçışlı yer tutucu (<KISI\\_1>) da "geri çevrilmemiş" sayılır."""
    g = G()
    d, _, _ = await _roundtrip(g, "Ben Ayşe Demir.", model_response("Merhaba <KISI_1>."))
    with pytest.raises(HTTPException):
        await g.async_post_call_success_hook(data=d, user_api_key_dict=None, response=model_response("Merhaba <KISI\\_1>."))


def test_completed_sessions_expire() -> None:
    from matriks_pii.guardrail import _CompletedSessions

    done = _CompletedSessions(ttl=-1.0)  # her kayıt anında süresi dolmuş sayılır
    done.put("a", {"<KISI_1>": "KISI"})
    done.put("b", {"<KISI_1>": "KISI"})  # yazarken süresi dolan "a" süpürülür
    assert len(done) == 1 and done.get("a") is None


async def test_deployment_hook_revert_marks_no_store() -> None:
    """Model düzeyi tanım: geri çevrilen (gerçek değerli) yanıt LiteLLM önbelleğine yazılmamalı."""
    from litellm.types.utils import CallTypes

    g = G()
    d = chat(f"Ben Ayşe Demir, TCKN {T1}.", guardrails=["matriks-pii"], cache={"ttl": 60})
    await pre(g, d)
    out = await g.async_post_call_success_deployment_hook(d, model_response("Merhaba <KISI_1>, TCKN <TCKN_1>."), CallTypes.acompletion)
    assert out.choices[0].message.content == f"Merhaba Ayşe Demir, TCKN {T1}."
    assert d["cache"] == {"ttl": 60, "no-store": True}
    # ardından proxy düzeyindeki tekrar çağrı işlem yapmadan geçer
    assert await g.async_post_call_success_hook(data=d, user_api_key_dict=None, response=out) is None


async def test_deployment_hook_without_pii_keeps_cache() -> None:
    from litellm.types.utils import CallTypes

    g = G()
    d = chat("Merhaba, hava nasıl?", guardrails=["matriks-pii"])
    await pre(g, d)
    out = await g.async_post_call_success_deployment_hook(d, model_response("Güzel."), CallTypes.acompletion)
    assert out is None and "cache" not in d


async def test_deployment_hook_not_model_level_is_noop() -> None:
    """`guardrails` listesi yoksa (yalnız default_on) dağıtım kancası hiçbir şey yapmaz: oturum proxy düzeyine kalır."""
    from litellm.types.utils import CallTypes

    g = G()
    d = chat(f"TCKN {T1}")
    await pre(g, d)
    resp = model_response("<TCKN_1>")
    assert await g.async_post_call_success_deployment_hook(d, resp, CallTypes.acompletion) is None
    assert "cache" not in d
    out = await g.async_post_call_success_hook(data=d, user_api_key_dict=None, response=resp)
    assert out.choices[0].message.content == T1


async def test_session_missing_closed_raises() -> None:
    g = G()
    d = chat("x")
    d["metadata"][SESSION_KEY] = {"matriks-pii": "matriks-pii:yok"}
    with pytest.raises(HTTPException) as ei:
        await g.async_post_call_success_hook(data=d, user_api_key_dict=None, response=model_response("<KISI_1>"))
    assert ei.value.status_code == 500 and ei.value.detail["error"] == "matriks_pii_session_missing"


async def test_session_missing_open_returns_none() -> None:
    g = G(revert_fail_mode="open")
    d = chat("x")
    d["metadata"][SESSION_KEY] = {"matriks-pii": "matriks-pii:yok"}
    assert await g.async_post_call_success_hook(data=d, user_api_key_dict=None, response=model_response("<KISI_1>")) is None


async def test_stream_flag_skips_success_hook() -> None:
    g = G()
    d = chat(f"TCKN {T1}", stream=True)
    await pre(g, d)
    assert await g.async_post_call_success_hook(data=d, user_api_key_dict=None, response=model_response("<TCKN_1>")) is None


async def test_placeholder_instruction_system_suffix() -> None:
    g = G(placeholder_instruction="system_suffix")
    d = {"model": "m", "metadata": {}, "messages": [{"role": "system", "content": "Sen bir asistansın."}, {"role": "user", "content": f"TCKN {T1}"}]}
    out = await pre(g, d)
    assert out["messages"][0]["content"].startswith("Sen bir asistansın.") and "yer tutucu" in out["messages"][0]["content"]
    d2 = chat("merhaba")
    out2 = await pre(g, d2)
    assert all(m["role"] != "system" for m in out2["messages"])  # PII yoksa talimat eklenmez


async def test_two_requests_isolated_and_concurrency() -> None:
    g = G()

    async def one(i: int) -> str:
        t = ["17291716060", "44771278268", "26197306992"][i % 3]
        d = chat(f"TCKN {t}")
        await pre(g, d)
        out = await g.async_post_call_success_hook(data=d, user_api_key_dict=None, response=model_response("<TCKN_1>"))
        return out.choices[0].message.content

    res = await asyncio.gather(*(one(i) for i in range(1000)))
    assert res == [["17291716060", "44771278268", "26197306992"][i % 3] for i in range(1000)]


async def test_hmac_ids_stable_across_requests() -> None:
    g = G(placeholder_id="hmac", hash_secret="test-secret")
    a = await pre(g, chat(f"TCKN {T1}"))
    b = await pre(g, chat(f"Yine {T1}"))
    pa = a["messages"][0]["content"].split()[-1]
    pb = b["messages"][0]["content"].split()[-1]
    assert pa == pb and pa.startswith("<TCKN_") and T1 not in pa


# ---------------------------------------------------------------- streaming
async def test_stream_iterator_reverts_and_trailing_usage() -> None:
    from litellm.types.utils import ModelResponseStream

    g = G()
    d = chat(f"Ben Ayşe Demir, TCKN {T1}", stream=True)
    await pre(g, d)
    chunks = stream_chunks("Tamam <KISI_1>'e, kimlik <TCKN_1> kayıtlı.", size=3, finish=False)
    usage_only = ModelResponseStream(choices=[])
    chunks.append(usage_only)
    acc, kinds = "", []
    async for c in g.async_post_call_streaming_iterator_hook(user_api_key_dict=None, response=agen(chunks), request_data=d):
        kinds.append(bool(c.choices))
        if c.choices:
            acc += c.choices[0].delta.content or ""
    assert acc == f"Tamam Ayşe Demir'e, kimlik {T1} kayıtlı."
    assert kinds[-1] is False  # usage parçası en sonda


async def test_stream_passthrough_without_session() -> None:
    g = G()
    d = chat("merhaba", stream=True)
    chunks = stream_chunks("<KISI_1>", 2)
    got = [c async for c in g.async_post_call_streaming_iterator_hook(user_api_key_dict=None, response=agen(chunks), request_data=d)]
    assert got == chunks


async def test_stream_revert_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    from matriks_pii import streaming

    g = G()
    d = chat(f"TCKN {T1}", stream=True)
    await pre(g, d)

    def boom(self: Any, item: Any) -> Any:
        raise RuntimeError("x")

    monkeypatch.setattr(streaming.StreamProcessor, "process", boom)
    with pytest.raises(HTTPException):
        async for _ in g.async_post_call_streaming_iterator_hook(user_api_key_dict=None, response=agen(stream_chunks("<TCKN_1>")), request_data=d):
            pass


# ---------------------------------------------------------------- apply_guardrail / logging_only / audit
async def test_apply_guardrail_roundtrip() -> None:
    g = G()
    req: dict[str, Any] = {"metadata": {}}
    masked = await g.apply_guardrail({"texts": [f"TCKN {T1}"]}, req, "request")
    assert masked["texts"] == ["TCKN <TCKN_1>"]
    back = await g.apply_guardrail({"texts": ["<TCKN_1> tamam"]}, req, "response")
    assert back["texts"] == [f"{T1} tamam"]


async def test_logging_hook_masks_copy_only() -> None:
    g = G()
    kwargs = {"messages": [{"role": "user", "content": f"TCKN {T1}"}]}
    resp = model_response(f"Yanıt {T2}")
    kw2, res2 = await g.async_logging_hook(kwargs, resp, "acompletion")
    assert kw2["messages"][0]["content"] == "TCKN <TCKN_1>" and res2.choices[0].message.content == "Yanıt <TCKN_2>"
    assert kwargs["messages"][0]["content"] == f"TCKN {T1}" and resp.choices[0].message.content == f"Yanıt {T2}"
    assert len(g.store) == 0  # type: ignore[arg-type]


async def test_audit_log_value_free(tmp_path: Any) -> None:
    g = G(audit_log={"enabled": True, "path": str(tmp_path / "audit.jsonl")})
    d = chat(f"Ben Ayşe Demir, TCKN {T1}")
    await pre(g, d)
    await g.async_post_call_success_hook(data=d, user_api_key_dict=None, response=model_response("<TCKN_1>"))
    files = list(tmp_path.glob("audit*.jsonl"))
    assert files
    lines = [json.loads(x) for f in files for x in f.read_text(encoding="utf-8").splitlines()]
    assert {r["event"] for r in lines} == {"pre_call", "post_call"}
    assert lines[0]["entities"] == {"KISI": 1, "TCKN": 1}
    no_secret(lines)


async def test_logging_hook_masks_standard_logging_object() -> None:
    g = G()
    slo = {"response": {"choices": [{"message": {"content": f"Ayşe Demir, TCKN {T1}"}}]}, "messages": [{"content": f"IBAN {IBAN1}"}], "id": "x"}
    kw2, _ = await g.async_logging_hook({"standard_logging_object": slo}, None, "acompletion")
    no_secret(kw2["standard_logging_object"])
    assert kw2["standard_logging_object"]["id"] == "x"
    assert T1 in json.dumps(slo, ensure_ascii=False)  # orijinal dokunulmadı


def test_env_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MATRIKS_TEST_SECRET", "gizli")
    g = G(placeholder_id="hmac", hash_secret="os.environ/MATRIKS_TEST_SECRET")
    assert g.cfg.hash_secret == "gizli"
    with pytest.raises(ValueError):
        G(placeholder_id="hmac", hash_secret="os.environ/YOK_BOYLE_BIR_DEGISKEN")


class _FakeRedis:
    def __init__(self) -> None:
        self.d: dict[str, bytes] = {}

    async def set(self, k: str, v: bytes, ex: int | None = None) -> None:
        self.d[k] = v

    async def get(self, k: str) -> bytes | None:
        return self.d.get(k)

    async def expire(self, k: str, ttl: int) -> None:
        return None

    async def delete(self, k: str) -> None:
        self.d.pop(k, None)


async def test_redis_store_encrypted_two_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    import base64
    import os

    from matriks_pii.store import RedisSessionStore

    monkeypatch.setenv("MATRIKS_PII_STORE_KEY", base64.b64encode(os.urandom(32)).decode())
    fake = _FakeRedis()
    shared = "s" * 40  # iki ayrı süreç/replika: aynı sır yapılandırmadan gelir
    g1, g2 = G(session_secret=shared), G(session_secret=shared)
    g1.store = RedisSessionStore(g1.cfg, "matriks-pii", client=fake)
    g2.store = RedisSessionStore(g2.cfg, "matriks-pii", client=fake)
    d = chat(f"Ben Ayşe Demir, TCKN {T1}")
    await pre(g1, d)
    for blob in fake.d.values():  # şifreli: düz metin değer yok
        assert T1.encode() not in blob and "Ayşe".encode() not in blob
    out = await g2.async_post_call_success_hook(data=d, user_api_key_dict=None, response=model_response("<KISI_1> <TCKN_1>"))
    assert out.choices[0].message.content == f"Ayşe Demir {T1}"
    assert fake.d == {}


async def test_json_key_gives_context() -> None:
    args = json.dumps({"ad": "Ayşe", "dogum_tarihi": "12.03.1985", "not": "Deniz kenarı"}, ensure_ascii=False)
    d = {"model": "m", "metadata": {}, "messages": [{"role": "assistant", "content": None, "tool_calls": [
        {"id": "1", "type": "function", "function": {"name": "kaydet", "arguments": args}}]}]}
    out = await pre(G(), d)
    got = json.loads(out["messages"][0]["tool_calls"][0]["function"]["arguments"])
    assert got == {"ad": "<KISI_1>", "dogum_tarihi": "<DOGUM_TARIHI_1>", "not": "Deniz kenarı"}


# ---------------------------------------------------------------- çoklu worker/replika sırları
SECRET = "x" * 48


def test_redis_requires_shared_session_secret_and_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from matriks_pii.config import TurkishPIIConfig

    monkeypatch.delenv("MATRIKS_PII_SESSION_SECRET", raising=False)
    monkeypatch.delenv("MATRIKS_PII_STORE_KEY", raising=False)
    with pytest.raises(ValueError, match="session_secret"):
        TurkishPIIConfig.build({"session_store": {"backend": "redis"}})
    with pytest.raises(ValueError, match="MATRIKS_PII_STORE_KEY"):
        TurkishPIIConfig.build({"session_store": {"backend": "redis"}, "session_secret": SECRET})
    with pytest.raises(ValueError, match="en az 32"):
        TurkishPIIConfig.build({"session_secret": "kisa"})
    cfg = TurkishPIIConfig.build({"session_store": {"backend": "redis", "encryption_key": "QUJD"}, "session_secret": SECRET})
    assert cfg.effective_session_secret == SECRET
    monkeypatch.setenv("MATRIKS_PII_SESSION_SECRET", SECRET)
    monkeypatch.setenv("MATRIKS_PII_STORE_KEY", "QUJD")
    assert TurkishPIIConfig.build({"session_store": {"backend": "redis"}}).effective_session_secret == SECRET


def test_session_secret_from_os_environ_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_SECRET", SECRET)
    g = G(session_secret="os.environ/MY_SECRET")
    assert g._secret == SECRET.encode()


async def test_token_valid_across_instances_only_with_same_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MATRIKS_PII_SESSION_SECRET", raising=False)
    d = chat(f"TCKN {T1}")
    g1 = G(session_secret=SECRET)
    await pre(g1, d)
    sid = g1._session_id(d)
    assert sid and G(session_secret=SECRET)._session_id(d) == sid  # başka replika aynı sırla doğrular
    assert G(session_secret="y" * 48)._session_id(d) is None       # farklı sır → oturum bulunamaz
    assert G()._session_id(d) is None                               # sır yoksa süreç başına rastgele


def test_secrets_not_in_version_hash() -> None:
    from matriks_pii.config import TurkishPIIConfig

    a = TurkishPIIConfig.build({"session_secret": SECRET, "placeholder_id": "hmac", "hash_secret": "h" * 32})
    b = TurkishPIIConfig.build({"session_secret": "z" * 48, "placeholder_id": "hmac", "hash_secret": "q" * 32})
    assert a.version_hash == b.version_hash  # replikalar arası config_hash eşit, sırdan bilgi taşımaz


def test_keys_cli_generates_valid_values() -> None:
    import base64

    from matriks_pii.config import TurkishPIIConfig
    from matriks_pii.keys import generate

    k = generate()
    assert len(base64.b64decode(k["MATRIKS_PII_STORE_KEY"])) == 32 and len(k["MATRIKS_PII_SESSION_SECRET"]) >= 32
    TurkishPIIConfig.build({"session_store": {"backend": "redis", "encryption_key": k["MATRIKS_PII_STORE_KEY"]},
                            "session_secret": k["MATRIKS_PII_SESSION_SECRET"]})
