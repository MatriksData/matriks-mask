"""LiteLLM CustomGuardrail: istekte maskele, yanıtta geri çevir.

config.yaml:
    guardrails:
      - guardrail_name: matriks-pii
        litellm_params:
          guardrail: matriks_pii.guardrail.TurkishPIIGuardrail
          mode: [pre_call, post_call]
          default_on: true
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import os
import threading
import time
import uuid
from collections import Counter, OrderedDict
from collections.abc import AsyncGenerator, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, ClassVar

from fastapi import HTTPException
from litellm._logging import verbose_proxy_logger
from litellm.integrations.custom_guardrail import CustomGuardrail
from litellm.types.guardrails import GuardrailEventHooks

from . import metrics
from .audit import AuditLog, api_key_hash, score_buckets
from .config import TurkishPIIConfig
from .engine import PIIEngine
from .recognizers.base import Span
from .revert import Reverter
from .session import MaskSession
from .store import StoreUnavailable, build_store
from .streaming import StreamProcessor
from .traverse import (
    TextRef,
    context_prefix,
    iter_request_texts,
    iter_response_texts,
    metadata_bucket,
    walk_leaves,
)

EMBEDDING_CALL_TYPES = frozenset({"embedding", "aembedding", "embeddings"})
SUPPORTED_CALL_TYPES = frozenset(
    {
        "completion", "acompletion", "text_completion", "atext_completion", "embedding", "aembedding", "embeddings",
        "responses", "aresponses", "anthropic_messages", "aanthropic_messages",
    }
)
GEMINI_CALL_TYPES = frozenset({"generate_content", "agenerate_content", "generate_content_stream", "agenerate_content_stream"})
SESSION_KEY = "matriks_pii_session_id"
SCRUBBED = "[matriks-pii: içerik güvenlik nedeniyle gizlendi]"
PLACEHOLDER_INSTRUCTION = (
    "Metinde <ETİKET_n> biçiminde yer tutucular (ör. <KISI_1>, <TCKN_1>) bulunabilir. Bunlar kişisel verilerdir ve "
    "yanıtın kullanıcıya ulaşmadan önce otomatik olarak gerçek değerlerle değiştirilir. Yer tutucuyu gerçek değermiş gibi "
    "kullan: değere atıf yaparken veya kullanıcı değeri tekrar etmeni istediğinde yer tutucuyu aynen yaz (ör. <TCKN_1>). "
    "Yer tutucudan, maskelemeden ya da değeri göremediğinden söz etme; yer tutucuyu kısaltma, değiştirme ve gerçek değeri "
    "tahmin etmeye çalışma."
)


class TextTooLarge(Exception):
    """Metin veya isteğin toplamı yapılandırılmış sınırı aşıyor (ileti değer içermez)."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"limit={limit}")
        self.limit = limit


def _debug_traceback_enabled() -> bool:
    """MATRIKS_PII_DEBUG_TRACEBACK=1: istisna yığını loglanır (yalnız sorun giderme; üretim modunda yok sayılır)."""
    if os.environ.get("MATRIKS_PII_DEBUG_TRACEBACK") != "1":
        return False
    return os.environ.get("LITELLM_MODE", "").upper() != "PRODUCTION"


def _log_error(where: str, e: BaseException) -> None:
    """Hata logu: varsayılan yalnız istisna tipi (ileti üçüncü taraf kütüphaneden kullanıcı metni taşıyabilir)."""
    if _debug_traceback_enabled():
        verbose_proxy_logger.exception("matriks_pii %s hatası: %s", where, type(e).__name__)
    else:
        verbose_proxy_logger.error("matriks_pii %s hatası: %s", where, type(e).__name__)


def _normalize_hooks(event_hook: Any) -> Any:
    if event_hook is None:
        return [GuardrailEventHooks.pre_call, GuardrailEventHooks.post_call]
    if isinstance(event_hook, str | GuardrailEventHooks):
        return [GuardrailEventHooks(event_hook)]
    if isinstance(event_hook, list):
        return [GuardrailEventHooks(h) for h in event_hook]
    return event_hook  # Mode (etiket tabanlı) — dokunma


def _binding(data: dict[str, Any]) -> str:
    """Jetonu isteğe bağla: aynı imzalı jeton başka bir isteğe kopyalanınca geçersiz olur."""
    md = data.get("litellm_metadata") if isinstance(data.get("litellm_metadata"), dict) else data.get("metadata")
    key = ""
    if isinstance(md, dict):
        key = str(md.get("user_api_key_hash") or md.get("user_api_key") or "")
    return f"{data.get('litellm_call_id') or ''}|{key}"


def _sign(secret: bytes, sid: str, binding: str) -> str:
    return sid + "." + hmac.new(secret, f"{sid}|{binding}".encode(), hashlib.sha256).hexdigest()[:32]


def _verify(secret: bytes, token: Any, binding: str) -> str | None:
    if not isinstance(token, str) or "." not in token:
        return None
    sid, _, sig = token.rpartition(".")
    good = hmac.new(secret, f"{sid}|{binding}".encode(), hashlib.sha256).hexdigest()[:32]
    return sid if hmac.compare_digest(sig, good) else None


class _RecentValues:
    """Geri çevrilmiş yanıt loglanırken kesin değiştirme için kısa ömürlü değer önbelleği (call_id → harita)."""

    def __init__(self, ttl: float = 60.0, max_items: int = 2_000) -> None:
        self.ttl, self.max = ttl, max_items
        self._d: OrderedDict[str, tuple[float, list[tuple[str, str]]]] = OrderedDict()
        self._lock = threading.Lock()

    def _sweep(self, now: float) -> None:
        while self._d:
            k, (ts, _) = next(iter(self._d.items()))
            if now - ts <= self.ttl:
                break
            self._d.pop(k, None)

    def put(self, call_id: str | None, session: MaskSession) -> None:
        if not call_id or not session.map:
            return
        pairs = sorted(((v, k) for k, v in session.map.items() if len(v) >= 3), key=lambda x: len(x[0]), reverse=True)
        now = time.monotonic()
        with self._lock:
            self._sweep(now)
            self._d[call_id] = (now, pairs)
            while len(self._d) > self.max:
                self._d.popitem(last=False)

    def get(self, call_id: str | None) -> list[tuple[str, str]]:
        """Silmeden okur (aynı çağrı için birden çok log geçişi olabilir); süresi dolanlar süpürülür."""
        if not call_id:
            return []
        now = time.monotonic()
        with self._lock:
            self._sweep(now)
            v = self._d.get(call_id)
        return v[1] if v else []

    def __len__(self) -> int:
        return len(self._d)


class _CompletedSessions:
    """Geri çevrilip silinen oturumların kısa ömürlü izi (oturum kimliği → yer tutucu: varlık; değer tutmaz).

    LiteLLM aynı yanıt için post_call kancasını birden çok kez çağırabilir (ör. guardrail hem `default_on` hem de model
    düzeyinde `guardrails: [...]` ile tanımlıysa). İlk çağrı oturumu siler; iz, sonraki çağrının "oturum kayıp" hatası
    yerine "zaten geri çevrildi" diye tanınmasını sağlar.
    """

    def __init__(self, ttl: float = 300.0, max_items: int = 10_000) -> None:
        self.ttl, self.max = ttl, max_items
        self._d: OrderedDict[str, tuple[float, dict[str, str]]] = OrderedDict()
        self._lock = threading.Lock()

    def _sweep(self, now: float) -> None:
        """Kayıtlar eklenme sırasındadır: süresi dolanlar baştan atılır."""
        while self._d:
            _, (ts, _) = next(iter(self._d.items()))
            if now - ts <= self.ttl:
                break
            self._d.popitem(last=False)

    def put(self, sid: str, entities: dict[str, str]) -> None:
        now = time.monotonic()
        with self._lock:
            self._sweep(now)
            self._d[sid] = (now, dict(entities))
            self._d.move_to_end(sid)
            while len(self._d) > self.max:
                self._d.popitem(last=False)

    def get(self, sid: str) -> dict[str, str] | None:
        with self._lock:
            self._sweep(time.monotonic())
            v = self._d.get(sid)
            return v[1] if v else None

    def __len__(self) -> int:
        return len(self._d)


class TurkishPIIGuardrail(CustomGuardrail):
    # apply_guardrail tanımladığımız hâlde LiteLLM'in bizim yaşam döngüsü kancalarımızı çağırması için
    use_native_lifecycle_hooks: ClassVar[bool] = True
    # apply_guardrail kendi değersiz kaydını yazar; LiteLLM'in otomatik sarmalayıcısı ayrıca kayıt (istisna metni dâhil) üretmesin
    records_own_guardrail_information: ClassVar[bool] = True

    def __init__(self, guardrail_name: str = "matriks-pii", event_hook: Any = None, default_on: bool = False, **kwargs: Any) -> None:
        cfg, rest = TurkishPIIConfig.from_kwargs(kwargs)
        hooks = _normalize_hooks(event_hook)
        if isinstance(hooks, list):
            if cfg.revert_response and GuardrailEventHooks.post_call not in hooks:
                hooks.append(GuardrailEventHooks.post_call)
                verbose_proxy_logger.warning("matriks_pii: mode listesine post_call eklendi (geri çevirme için zorunlu)")
            if GuardrailEventHooks.pre_call not in hooks:
                hooks.insert(0, GuardrailEventHooks.pre_call)
                verbose_proxy_logger.warning("matriks_pii: mode listesine pre_call eklendi")
            if cfg.mask_logs and GuardrailEventHooks.logging_only not in hooks:
                hooks.append(GuardrailEventHooks.logging_only)  # loglanan yanıt/istek kopyası yeniden maskelenir
        else:
            verbose_proxy_logger.warning("matriks_pii: etiket tabanlı mode; pre_call ve post_call birlikte tanımlanmalı")
        unknown = [k for k in rest if k not in _CUSTOM_GUARDRAIL_KWARGS]
        if unknown:
            verbose_proxy_logger.warning("matriks_pii: tanınmayan parametreler (yazım hatası olabilir) CustomGuardrail'e iletildi: %s", sorted(unknown))
        super().__init__(guardrail_name=guardrail_name, event_hook=hooks, default_on=default_on, **rest)
        self.cfg = cfg
        self.guardrail_provider = "matriks_pii"
        self.engine = PIIEngine(cfg)
        if cfg.name_detection.ner_backend != "none" and cfg.name_detection.ner_preload:
            for r in self.engine.recognizers:
                if hasattr(r, "preload"):
                    r.preload()  # yüklenemezse RuntimeError: proxy başlamaz (sessiz NER'siz çalışma yok)
        self.store = build_store(cfg, namespace=guardrail_name or "matriks-pii")
        secret = cfg.effective_session_secret
        # memory deposunda pre/post aynı süreçte çalışır → süreç başına rastgele sır yeterli; redis'te sır zorunludur (config)
        self._secret = secret.encode() if secret else os.urandom(32)
        prev = cfg.effective_session_secret_previous
        self._secret_prev = prev.encode() if prev else None  # rotasyon: yalnız doğrulama
        self.audit = AuditLog(cfg.audit_log.enabled, cfg.audit_log.path, cfg.version_hash)
        self._audit_err_at = -1e9
        if os.environ.get("MATRIKS_PII_DEBUG_TRACEBACK") == "1" and not _debug_traceback_enabled():
            verbose_proxy_logger.warning("matriks_pii: MATRIKS_PII_DEBUG_TRACEBACK üretim modunda (LITELLM_MODE=PRODUCTION) yok sayıldı")
        self._recent = _RecentValues()
        self._completed = _CompletedSessions()
        self._repeat_warned = False
        self._no_store_warned = False
        # analiz ve log maskeleme CPU işidir: LiteLLM'in varsayılan executor'ını aç bırakmamak için ayrılmış havuz
        self._executor = ThreadPoolExecutor(max_workers=cfg.analysis_threads, thread_name_prefix="matriks-pii")

    # ================================================================== oturum kimliği
    def _session_id(self, data: dict[str, Any]) -> str | None:
        """İmzalı oturum kimliği; istemcinin gönderdiği/uydurduğu değerler imza doğrulamasından geçmez."""
        for bucket in ("litellm_metadata", "metadata"):
            md = data.get(bucket)
            if isinstance(md, dict) and isinstance(md.get(SESSION_KEY), dict):
                token, binding = md[SESSION_KEY].get(self.guardrail_name), _binding(data)
                sid = _verify(self._secret, token, binding)
                if sid is None and self._secret_prev is not None:
                    sid = _verify(self._secret_prev, token, binding)
                if sid is not None:
                    return sid
        return None

    def _set_session_id(self, data: dict[str, Any], call_type: str | None, sid: str) -> None:
        token = _sign(self._secret, sid, _binding(data))
        buckets = [metadata_bucket(data, call_type)]
        # istemcinin diğer kovaya koyduğu değer bizimkini gölgelemesin: o kovada da üzerine yaz
        for b in ("litellm_metadata", "metadata"):
            md = data.get(b)
            if isinstance(md, dict) and isinstance(md.get(SESSION_KEY), dict) and md not in buckets:
                buckets.append(md)
        for bucket in buckets:
            cur = bucket.get(SESSION_KEY)
            if not isinstance(cur, dict):
                cur = bucket[SESSION_KEY] = {}
            cur[self.guardrail_name] = token

    def _token_present(self, data: dict[str, Any]) -> bool:
        """Bu guardrail'in oturum jetonu istekte var mı? (İmza doğrulanmasa bile — sır uyuşmazlığını da yakalar.)"""
        for bucket in ("litellm_metadata", "metadata"):
            md = data.get(bucket)
            if isinstance(md, dict) and isinstance(md.get(SESSION_KEY), dict) and self.guardrail_name in md[SESSION_KEY]:
                return True
        return False

    def _session_missing(self, data: dict[str, Any], user_api_key_dict: Any, event: str) -> None:
        """Jeton var ama oturum yok: yanıt yer tutucularla istemciye gitmesin (fail-closed)."""
        self.audit.count("session_missing")
        self._ev("session_missing")
        verbose_proxy_logger.error(
            "matriks_pii: oturum bulunamadı/doğrulanamadı (TTL dolmuş, depo erişilemiyor, sır uyuşmuyor veya farklı süreç). "
            "event=%s", event,
        )
        self._audit(data, user_api_key_dict, None, "guardrail_failed_to_respond", None, 0.0, event=event, session_missing=True)
        if self.cfg.revert_fail_mode == "closed":
            raise HTTPException(status_code=500, detail={"error": "matriks_pii_session_missing", "guardrail": self.guardrail_name})

    def _store_unavailable(self, err: StoreUnavailable) -> HTTPException:
        self._ev("store_unavailable")
        verbose_proxy_logger.error("matriks_pii: oturum deposu erişilemiyor (%s)", err)
        return HTTPException(status_code=503, detail={"error": "matriks_pii_store_unavailable", "guardrail": self.guardrail_name})

    async def _safe_pop(self, sid: str | None) -> None:
        """Oturumu sil; depo hatası yanıtı bozmaz (TTL zaten temizler)."""
        if not sid:
            return
        try:
            await self.store.pop(sid)
        except Exception as e:
            verbose_proxy_logger.warning("matriks_pii: oturum silinemedi (%s)", type(e).__name__)

    def _already_reverted(self, sid: str | None, response: Any) -> bool:
        """Bu oturum az önce geri çevrilip silindi mi ve yanıtta yer tutucusu kalmadı mı?

        Yanıtta hâlâ yer tutucu varsa `False` döner: çağıran "oturum kayıp" yoluna gider (fail-closed korunur).
        """
        entities = self._completed.get(sid) if sid else None
        if entities is None:
            return False
        # Yer tutucuyu kendisiyle eşleyen geri çevirici: değer taşımaz, yalnız "bu oturumun yer tutucusu kaldı mı"
        # sorusunu asıl geri çeviriciyle aynı tarayıcıyla (kaçışlı ve numarasız yazımlar dâhil) yanıtlar.
        probe = Reverter({ph: ph for ph in entities}, entities, self.cfg)
        target = response.model_copy(deep=True) if hasattr(response, "model_copy") else copy.deepcopy(response)
        commits: list[Callable[[], None]] = []
        for ref in iter_response_texts(target, self.cfg, commits, preserve=True):
            text = ref.get()
            if isinstance(text, str) and text:
                probe.revert(text)
        if probe.stats.replaced > 0:
            return False
        if not self._repeat_warned:
            self._repeat_warned = True
            verbose_proxy_logger.warning(
                "matriks_pii: aynı yanıt için post_call birden çok kez çağrıldı; guardrail hem default_on hem de model/"
                "anahtar düzeyinde tanımlı olabilir (çift tanımı kaldırın). Tekrar çağrı işlem yapmadan geçildi."
            )
        return True

    async def _session_from(self, data: dict[str, Any]) -> MaskSession | None:
        sid = self._session_id(data)
        return await self.store.get(sid) if sid else None

    def _reverter(self, session: MaskSession) -> Reverter:
        return Reverter(session.map, session.entities, self.cfg, reserved=session.reserved, wrapped=session.wrapped)

    @property
    def _skip_system(self) -> bool:
        return bool(getattr(self, "skip_system_message_in_guardrail", False))

    # ================================================================== analiz
    def _analyze_refs(self, refs: list[TextRef], texts: list[Any]) -> list[list[Span]]:
        return [self.engine.analyze_with_prefix(t, context_prefix(r)) if isinstance(t, str) and t else []
                for r, t in zip(refs, texts, strict=True)]

    def _check_size(self, texts: list[Any]) -> int:
        total = 0
        for t in texts:
            if isinstance(t, str):
                if len(t) > self.cfg.max_text_chars:
                    raise TextTooLarge(self.cfg.max_text_chars)
                total += len(t)
        if total > self.cfg.max_request_chars:
            raise TextTooLarge(self.cfg.max_request_chars)
        return total

    async def _in_executor(self, fn: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(self._executor, fn, *args)

    async def _analyze_all(self, refs: list[TextRef], texts: list[Any]) -> list[list[Span]]:
        total = self._check_size(texts)
        # NER (torch) kısa metinde bile on/yüzlerce ms CPU → olay döngüsü bloklanmasın diye her zaman havuzda
        if total > self.cfg.executor_threshold_chars or (total and self.cfg.name_detection.ner_backend != "none"):
            return await self._in_executor(self._analyze_refs, refs, texts)  # type: ignore[no-any-return]
        return self._analyze_refs(refs, texts)

    def _too_large(self, data: dict[str, Any], ct: str, err: TextTooLarge, generic: bool = False) -> HTTPException:
        """Büyük metin her durumda (fail_mode'dan bağımsız) ham gitmez: istek temizlenir, 413."""
        self._ev("text_too_large")
        self._scrub(data, ct, generic=generic)
        return HTTPException(status_code=413, detail={"error": "matriks_pii_text_too_large", "limit": err.limit,
                                                      "guardrail": self.guardrail_name})

    # ================================================================== loglama
    def _ev(self, event: str, n: int = 1) -> None:
        if self.cfg.metrics_enabled:
            metrics.inc_event(self.guardrail_name, event, n)

    def _metrics_for(self, event: GuardrailEventHooks, status: str, dur: float, session: MaskSession | None,
                     extra: dict[str, Any] | None, spans: list[Span] | None) -> None:
        if not self.cfg.metrics_enabled:
            return
        hook = getattr(event, "value", str(event))
        metrics.observe_hook(self.guardrail_name, hook, status, dur)
        if session is not None and hook == "pre_call" and not (extra or {}).get("dry_run"):
            metrics.inc_entities(self.guardrail_name, session.entity_counts, self.cfg.action)
            metrics.inc_entities(self.guardrail_name, session.log_counts, self.cfg.action)
            metrics.inc_score_buckets(self.guardrail_name, score_buckets(spans))
        if hook == "pre_call":
            if hasattr(self.store, "__len__"):
                metrics.set_gauge("sessions_open", self.guardrail_name, len(self.store))  # type: ignore[arg-type]
            metrics.set_gauge("cache_entries", self.guardrail_name, len(self.engine.cache))
        if extra and extra.get("blocked"):
            self._ev("blocked")
        if extra and extra.get("unresolved"):
            self._ev("unresolved_placeholder", int(extra["unresolved"]))

    def _log(self, data: dict[str, Any], status: str, session: MaskSession | None, t0: float,
             event: GuardrailEventHooks, extra: dict[str, Any] | None = None, spans: list[Span] | None = None) -> None:
        """Değersiz loglama: yalnız sayımlar, entity adları ve offset'ler."""
        t1 = time.time()
        dur = time.monotonic() - t0
        self._metrics_for(event, status, dur, session, extra, spans)
        masked = dict(session.entity_counts) if session else {}
        logged = dict(session.log_counts) if session else {}
        resp: dict[str, Any] = {"entities": masked, "logged": logged}
        if extra:
            resp.update(extra)
        blocked = extra.get("blocked") if extra else None
        detail: dict[str, Any] = {
            "detection_method": "regex+checksum+lexicon" + ("+ner" if self.cfg.name_detection.ner_backend != "none" else ""),
            "match_details": [
                {"entity": s.entity, "start": s.start, "end": s.end, "score": s.score, "recognizer": s.recognizer}
                for s in (spans or [])[:200]
            ],
            "patterns_checked": len(self.engine.recognizers),
            "violation_categories": sorted(set(masked) | ({blocked} if blocked else set())),
            "guardrail_action": "BLOCK" if blocked else ("MASK" if masked else "NONE"),
        }
        kwargs: dict[str, Any] = {
            "guardrail_json_response": resp, "request_data": data, "guardrail_status": status,
            "start_time": t1 - dur, "end_time": t1, "duration": dur, "masked_entity_count": masked,
        }
        log_fn: Any = self.add_standard_logging_guardrail_information_to_request_data
        try:
            log_fn(guardrail_provider=self.guardrail_provider, event_type=event, tracing_detail=detail, **kwargs)
        except TypeError:  # eski LiteLLM sürümleri: bu üç parametre yok
            try:
                log_fn(**kwargs)
            except Exception as e:  # pragma: no cover
                verbose_proxy_logger.debug("matriks_pii log hatası: %s", type(e).__name__)
        except Exception as e:  # pragma: no cover
            verbose_proxy_logger.debug("matriks_pii log hatası: %s", type(e).__name__)

    def _audit(self, data: dict[str, Any], user_api_key_dict: Any, call_type: str | None, status: str,
               session: MaskSession | None, dur_ms: float, **extra: Any) -> None:
        if not self.audit.enabled:
            return
        md = metadata_bucket(data, call_type) if isinstance(data, dict) else {}
        sid = self._session_id(data) if isinstance(data, dict) else None
        rec = {
            "event": extra.pop("event", "pre_call"),
            "litellm_call_id": data.get("litellm_call_id"),
            "session_ref": hashlib.sha256(sid.encode()).hexdigest()[:16] if sid else None,  # kimliğin kendisi değil
            "api_key_hash": api_key_hash(getattr(user_api_key_dict, "api_key", None) or getattr(user_api_key_dict, "token", None)),
            "user_id": md.get("user_api_key_user_id") or getattr(user_api_key_dict, "user_id", None),
            "team_id": md.get("user_api_key_team_id") or getattr(user_api_key_dict, "team_id", None),
            "model": data.get("model"),
            "call_type": call_type,
            "stream": bool(data.get("stream")),
            "entities": dict(session.entity_counts) if session else {},
            "logged": dict(session.log_counts) if session else {},
            "actions": {e: self.cfg.action(e) for e in (session.entity_counts | session.log_counts)} if session else {},
            "status": status,
            "duration_ms": round(dur_ms, 3),
            "guardrail": self.guardrail_name,
        }
        rec.update(extra)
        try:
            self.audit.write(rec)
        except Exception as e:  # denetim izi yazılamazsa isteği bozma; log taşkınını önlemek için 60 sn'de bir
            self._ev("audit_write_failed")
            self.audit.count("write_failed")
            now = time.monotonic()
            if now - self._audit_err_at >= 60:
                self._audit_err_at = now
                verbose_proxy_logger.error("matriks_pii audit yazılamadı: %s (60 sn boyunca tekrar loglanmaz)", type(e).__name__)

    # ================================================================== PRE-CALL
    async def async_pre_call_hook(self, user_api_key_dict: Any, cache: Any, data: dict[str, Any], call_type: Any) -> Any:
        ct = str(getattr(call_type, "value", call_type))
        if not self.should_run_guardrail(data=data, event_type=GuardrailEventHooks.pre_call):
            return None
        if ct not in SUPPORTED_CALL_TYPES and not (ct in GEMINI_CALL_TYPES and self.cfg.gemini_native):
            return await self._unsupported_call(user_api_key_dict, data, ct)
        t0 = time.monotonic()
        status = "success"
        blocked: str | None = None
        all_spans: list[Span] = []
        session = MaskSession(self.cfg)
        reuse = False
        commits: list[Callable[[], None]] = []
        refs: list[TextRef] = []
        masked_done = False
        log_extra: dict[str, Any] = {}
        try:
            # aynı istekte ikinci kez çağrılırsak (ör. vector store bağlamı eklendiğinde) mevcut oturumu sürdür
            existing_sid = self._session_id(data)
            prev = (await self.store.get(existing_sid)) if existing_sid else None
            if prev is not None:
                session, reuse = prev, True
            refs = list(iter_request_texts(data, ct, self.cfg, commits, skip_system=self._skip_system))
            texts = [r.get() for r in refs]
            session.reserve_existing(t for t in texts if isinstance(t, str))
            analyses = await self._analyze_all(refs, texts)
            if self.cfg.dry_run:
                # gölge mod: hiçbir metin değişmez, oturum/jeton yazılmaz, BLOCK uygulanmaz; yalnız sayım loglanır
                would: Counter[str] = Counter()
                for spans in analyses:
                    to_mask, blk, to_log = self.engine.actionable(spans)
                    session.count_log(to_log)
                    for s in to_mask + blk:
                        would[s.entity] += 1
                    if blk and blocked is None:
                        blocked = blk[0].entity
                    all_spans.extend(to_mask + blk)
                log_extra = {"dry_run": True, "would_mask": dict(would), "would_block": blocked}
                self._ev("dry_run_would_mask", sum(would.values()))
                blocked = None
                return None
            for spans in analyses:  # 1) BLOCK kararı
                _, blk, _ = self.engine.actionable(spans)
                if blk:
                    blocked = blk[0].entity
                    break
            mask_blocked = blocked is not None
            if blocked and ct in EMBEDDING_CALL_TYPES:
                # embedding metin döndürmez: isteği reddetmek yerine engellenecek değer de maskelenip geçirilir
                self._ev("blocked_masked_embedding")
                blocked = None
            # 2) maskele (yerinde). BLOCK olsa bile: hata yolu isteği loglar → engellenen değer dahil gizle
            for ref, text, spans in zip(refs, texts, analyses, strict=True):
                to_mask, blk, to_log = self.engine.actionable(spans)
                session.count_log(to_log)
                spans_to_mask = to_mask + blk if mask_blocked else to_mask
                all_spans.extend(spans_to_mask)
                if spans_to_mask:
                    masked = self.engine.mask(text, spans_to_mask, session)
                    if ref.kind == "json_number" and masked in session.map:
                        session.numeric.add(masked)
                    ref.set(masked)
            for c in commits:
                c()
            self._mask_proxy_body(data, ct, session, include_block=mask_blocked)
            if blocked:
                status = "guardrail_intervened"
                raise HTTPException(
                    status_code=400,
                    detail={"error": "matriks_pii_blocked", "guardrail": self.guardrail_name, "entity": blocked},
                )
            masked_done = True
            if self.cfg.placeholder_instruction == "system_suffix" and session.map and not reuse:
                _inject_instruction(data, ct)
            if reuse and existing_sid:
                await self.store.put(existing_sid, session)
            elif session.map or self.cfg.scan_response:
                # geri çevrilecek bir şey yoksa oturum/jeton yazılmaz: depo kesintisi PII'siz istekleri etkilemez
                sid = f"{self.guardrail_name}:{uuid.uuid4().hex}"
                await self.store.put(sid, session)
                self._set_session_id(data, ct, sid)
            if self.cfg.store_map_in_metadata:
                metadata_bucket(data, ct)["matriks_pii_debug_map"] = dict(session.map)
            if session.map or session.log_counts:
                status = "guardrail_intervened"
            _mark_applied(data, self.guardrail_name)
            return data
        except HTTPException:
            raise
        except TextTooLarge as e:
            status = "guardrail_failed_to_respond"
            if self.cfg.dry_run:  # gölge mod hiçbir isteği durdurmaz/değiştirmez; yalnız sayılır
                self._ev("text_too_large")
                log_extra = {"dry_run": True, "too_large": True}
                return None
            raise self._too_large(data, ct, e) from None
        except StoreUnavailable as e:
            status = "guardrail_failed_to_respond"
            err = self._store_unavailable(e)
            if self.cfg.fail_mode == "closed":
                self._scrub(data, ct)
                raise err from None
            return data if masked_done else None  # open: maskeli istek gider ama geri çevrilemez
        except Exception as e:
            status = "guardrail_failed_to_respond"
            _log_error("pre_call", e)
            if self.cfg.dry_run:  # gölge mod: hata isteği etkilemez (ölçülür: status + metrik)
                log_extra = {"dry_run": True}
                return None
            if self.cfg.fail_mode == "closed":
                self._scrub(data, ct)  # hata yolunda loglanacak istek ham veri taşımasın
                raise HTTPException(status_code=500, detail={"error": "matriks_pii_guardrail_failure", "guardrail": self.guardrail_name}) from None
            return None
        finally:
            extra = {"blocked": blocked, "reserved_placeholders": len(session.reserved), **log_extra}
            self._log(data, status, session, t0, GuardrailEventHooks.pre_call, extra, all_spans)
            self._audit(data, user_api_key_dict, ct, status, session, (time.monotonic() - t0) * 1000, blocked=blocked,
                        score_buckets=score_buckets(all_spans), **log_extra)

    def _scrub(self, data: dict[str, Any], call_type: str, generic: bool = False) -> None:
        """Fail-closed hata yolunda tüm metin alanlarını sabit uyarıyla değiştir (en iyi çaba)."""
        try:
            for body in [data] + self._proxy_bodies(data):
                commits: list[Callable[[], None]] = []
                for ref in list(iter_request_texts(body, call_type, self.cfg, commits, generic=generic)):
                    t = ref.get()
                    if isinstance(t, str) and t:
                        ref.set(SCRUBBED)
                for c in commits:
                    c()
        except Exception:  # pragma: no cover
            for k in ("messages", "input", "prompt", "system", "instructions", "query", "documents", "text", "contents"):
                if k in data:
                    data[k] = SCRUBBED

    async def _unsupported_call(self, user_api_key_dict: Any, data: dict[str, Any], ct: str) -> Any:
        """rerank, görsel, ses, MCP... : geri çevirme yok; varsayılan olarak metin alanları maskelenir."""
        mode = self.cfg.unsupported_call_types
        if mode == "pass":
            return None
        t0 = time.monotonic()
        session = MaskSession(self.cfg)
        status = "success"
        blocked: str | None = None
        try:
            refs = list(iter_request_texts(data, ct, self.cfg, None, generic=True))
            texts = [r.get() for r in refs]
            analyses = await self._analyze_all(refs, texts)
            found = [s for spans in analyses for s in spans if self.cfg.action(s.entity) in ("MASK", "BLOCK")]
            for ref, text, spans in zip(refs, texts, analyses, strict=True):
                to_mask, blk, to_log = self.engine.actionable(spans)
                session.count_log(to_log)
                if to_mask or blk:
                    ref.set(self.engine.mask(text, to_mask + blk, session))
            if found and mode == "block":  # önce maskele: hata yolu isteği loglar
                status, blocked = "guardrail_intervened", found[0].entity
                raise HTTPException(status_code=400, detail={"error": "matriks_pii_blocked", "guardrail": self.guardrail_name,
                                                             "entity": found[0].entity})
            if session.map or session.log_counts:
                status = "guardrail_intervened"
            return data
        except HTTPException:
            raise
        except TextTooLarge as e:
            status = "guardrail_failed_to_respond"
            raise self._too_large(data, ct, e, generic=True) from None
        except Exception as e:
            status = "guardrail_failed_to_respond"
            _log_error("unsupported pre_call", e)
            if self.cfg.fail_mode == "closed":
                self._scrub(data, ct, generic=True)
                raise HTTPException(status_code=500, detail={"error": "matriks_pii_guardrail_failure", "guardrail": self.guardrail_name}) from None
            return None
        finally:
            self._log(data, status, session, t0, GuardrailEventHooks.pre_call, {"blocked": blocked, "unsupported_call": True})
            self._audit(data, user_api_key_dict, ct, status, session, (time.monotonic() - t0) * 1000,
                        event="pre_call_unsupported", blocked=blocked)

    @staticmethod
    def _proxy_bodies(data: dict[str, Any]) -> list[dict[str, Any]]:
        bodies = []
        psr = data.get("proxy_server_request")
        if isinstance(psr, dict) and isinstance(psr.get("body"), dict):
            bodies.append(psr["body"])
        for b in ("metadata", "litellm_metadata"):
            md = data.get(b)
            if isinstance(md, dict):
                p = md.get("proxy_server_request")
                if isinstance(p, dict) and isinstance(p.get("body"), dict):
                    bodies.append(p["body"])
        return bodies

    def _mask_proxy_body(self, data: dict[str, Any], call_type: str, session: MaskSession, include_block: bool = False) -> None:
        """Proxy'nin guardrail'den önce aldığı gövde kopyası da maskelenir (idempotent)."""
        for body in self._proxy_bodies(data):
            commits: list[Callable[[], None]] = []
            for ref in list(iter_request_texts(body, call_type, self.cfg, commits, skip_system=self._skip_system)):
                t = ref.get()
                if not isinstance(t, str) or not t:
                    continue
                to_mask, blk, _ = self.engine.actionable(self.engine.analyze_with_prefix(t, context_prefix(ref)))
                spans = to_mask + blk if include_block else to_mask
                if spans:
                    ref.set(self.engine.mask(t, spans, session, count=False))  # aynı isteğin kopyası: ikinci kez sayılmaz
            for c in commits:
                c()

    # ================================================================== POST-CALL
    def _revert_obj(self, rev: Reverter, numeric: frozenset[str] = frozenset()) -> Callable[[Any, str], Any]:
        cfg = self.cfg

        def _fn(obj: Any, kind: str) -> Any:
            target = obj.model_copy(deep=True) if hasattr(obj, "model_copy") else copy.deepcopy(obj)
            commits: list[Callable[[], None]] = []
            refs = (list(walk_leaves(target, "json", "assistant")) if kind == "json"
                    else list(iter_response_texts(target, cfg, commits, preserve=True)))
            for ref in refs:
                t = ref.get()
                if isinstance(t, str) and t:
                    restored = rev.revert(t, json_escape=ref.kind == "text_json")
                    if t.strip() in numeric and restored.strip().lstrip("-").isdigit():
                        ref.set(int(restored.strip()))  # type: ignore[arg-type]  # istekte sayıydı → sayı olarak dön
                    else:
                        ref.set(restored)
            for c in commits:
                c()
            if kind == "item":
                return target["output"][0]
            if kind == "part":
                return target["output"][0]["content"][0]
            return target

        return _fn

    async def async_post_call_success_hook(self, data: dict[str, Any], user_api_key_dict: Any, response: Any) -> Any:
        if not self.cfg.revert_response:
            return None
        if not self.should_run_guardrail(data=data, event_type=GuardrailEventHooks.post_call):
            return None
        if data.get("stream"):
            return None  # akış kancası geri çevirdi; bu çağrı yalnız birleştirilmiş denetim kopyası içindir
        sid = self._session_id(data)
        try:
            session = await self.store.get(sid) if sid else None
        except StoreUnavailable as e:
            err = self._store_unavailable(e)
            if self.cfg.revert_fail_mode == "closed":
                raise err from None
            return None
        if session is None:
            if self._already_reverted(sid, response):
                self._ev("post_call_repeat")
                return None
            if self._token_present(data):
                self._session_missing(data, user_api_key_dict, "post_call")
            return None
        t0 = time.monotonic()
        try:
            if not session.map and not self.cfg.scan_response:
                await self._safe_pop(sid)
                return None
            rev = self._reverter(session)
            resp = self._revert_obj(rev, frozenset(session.numeric))(response, "response")
            new_pii = self._count_new_pii(resp, session) if self.cfg.response_leak_report else 0
            scanned = self._scan_response(resp, session) if self.cfg.scan_response else 0
            if self.cfg.mask_logs:
                self._recent.put(data.get("litellm_call_id"), session)
            if sid:
                self._completed.put(sid, session.entities)
            await self._safe_pop(sid)
            self._metrics_for(GuardrailEventHooks.post_call, "success", time.monotonic() - t0, session,
                              {"unresolved": rev.stats.unresolved}, None)
            self._audit(
                data, user_api_key_dict, None, "success", session, (time.monotonic() - t0) * 1000,
                event="post_call", unresolved=rev.stats.unresolved, replaced=rev.stats.replaced, new_pii=new_pii, scanned=scanned,
            )
            return resp
        except Exception as e:
            _log_error("post_call", e)
            if self.cfg.revert_fail_mode == "closed":
                raise HTTPException(status_code=500, detail={"error": "matriks_pii_revert_failure", "guardrail": self.guardrail_name}) from None
            return None

    async def async_post_call_success_deployment_hook(self, request_data: dict[str, Any], response: Any, call_type: Any) -> Any:
        """Model düzeyi tanımda (`litellm_params.guardrails`) geri çevrilen yanıt LiteLLM önbelleğine yazılmaz.

        LiteLLM bu kancayı yanıtı önbelleğe yazmadan ÖNCE çağırır; buradan dönen yanıt gerçek değer taşır. Önbellek
        anahtarı ise maskeli istekten üretilir ("Ben <KISI_1>, TCKN <TCKN_1>" herkes için aynıdır): yanıt önbelleğe
        yazılırsa aynı maskeli isteği gönderen başka bir kullanıcı bu değerleri alır. Bu yüzden geri çevirme yapıldıysa
        çağrı `no-store` işaretlenir. Yalnız `default_on` / istek düzeyi tanımda bu kanca çalışmaz (önbellekte yer
        tutuculu yanıt durur, her kullanıcı kendi değerlerini alır).
        """
        out = await super().async_post_call_success_deployment_hook(request_data, response, call_type)
        if out is not None and isinstance(request_data, dict):
            cache = request_data.get("cache")
            request_data["cache"] = {**(cache if isinstance(cache, dict) else {}), "no-store": True}
            self._ev("deployment_revert_no_store")
            if not self._no_store_warned:
                self._no_store_warned = True
                verbose_proxy_logger.warning(
                    "matriks_pii: guardrail model düzeyinde (litellm_params.guardrails) tanımlı; geri çevrilen yanıt "
                    "önbelleğe yazılmadı (no-store). Model düzeyi tanımı kaldırıp default_on ya da istek/anahtar düzeyi "
                    "tanım kullanın."
                )
        return out

    async def async_post_call_failure_hook(self, request_data: dict[str, Any], original_exception: Exception,
                                           user_api_key_dict: Any, traceback_str: str | None = None) -> Any:
        """LLM çağrısı başarısız: oturumu hemen sil (değerler TTL boyunca bellekte kalmasın)."""
        try:
            sid = self._session_id(request_data) if isinstance(request_data, dict) else None
            if sid:
                await self.store.pop(sid)
        except Exception as e:  # pragma: no cover
            verbose_proxy_logger.debug("matriks_pii failure_hook: %s", type(e).__name__)
        return None

    def _scan_response(self, resp: Any, session: MaskSession) -> int:
        """Yanıtta LLM'in ürettiği YENİ kişisel veriyi (istekte olmayan) maskeler; bunlar geri çevrilmez.

        İstekten gelip geri yüklenen değerler (oturum haritası) dokunulmadan kalır. Numaralama oturumdan devam eder
        (ör. istekte <KISI_1> vardıysa yanıttaki yeni kişi <KISI_2> olur). Akışta uygulanmaz.
        """
        known = set(session.map.values())
        n = 0
        commits: list[Callable[[], None]] = []
        for ref in list(iter_response_texts(resp, self.cfg, commits)):
            t = ref.get()
            if not isinstance(t, str) or not t:
                continue
            spans = self.engine.analyze_with_prefix(t, context_prefix(ref))
            to_mask, blk, _ = self.engine.actionable(spans)
            new = [s for s in to_mask + blk if s.text not in known]
            if new:
                n += len(new)
                ref.set(self.engine.mask(t, new, session))
        for c in commits:
            c()
        return n

    def _count_new_pii(self, resp: Any, session: MaskSession) -> int:
        """Yanıtta haritada olmayan yeni kesin tanımlayıcı sayısı (maskelemez; yalnız sayar)."""
        known = set(session.map.values())
        n = 0
        for ref in iter_response_texts(resp, self.cfg):
            t = ref.get()
            if not isinstance(t, str):
                continue
            for s in self.engine.analyze(t):
                if s.entity in ("TCKN", "IBAN", "KART", "VKN", "TELEFON", "EPOSTA") and s.text not in known:
                    n += 1
        return n

    # ================================================================== STREAMING
    async def async_post_call_streaming_iterator_hook(
        self, user_api_key_dict: Any, response: Any, request_data: dict[str, Any]
    ) -> AsyncGenerator[Any, None]:
        session = None
        sid = self._session_id(request_data) if isinstance(request_data, dict) else None
        active = (isinstance(request_data, dict) and self.cfg.revert_response
                  and self.should_run_guardrail(data=request_data, event_type=GuardrailEventHooks.post_call))
        if sid and active:
            try:
                session = await self.store.get(sid)
            except StoreUnavailable as e:
                err = self._store_unavailable(e)
                if self.cfg.revert_fail_mode == "closed":
                    raise err from None
        if session is None and active and self._token_present(request_data):
            self._session_missing(request_data, user_api_key_dict, "post_call_stream")  # closed → ilk parçadan önce
        if session is None or not session.map:
            async for item in response:
                yield item
            if sid and session is not None:
                await self._safe_pop(sid)
            return
        t_stream = time.monotonic()
        rev = self._reverter(session)
        proc = StreamProcessor(rev, self.cfg.streaming.holdback_max_chars, self.cfg.tool_restore_policy == "all",
                               revert_obj=self._revert_obj(rev, frozenset(session.numeric)))
        trailing: list[Any] = []
        failed = False
        try:
            async for item in response:
                if failed:
                    yield item
                    continue
                try:
                    outs = proc.process(item)
                except Exception as e:
                    _log_error("stream", e)
                    if self.cfg.revert_fail_mode == "closed":
                        raise HTTPException(status_code=500, detail={"error": "matriks_pii_revert_failure"}) from None
                    failed = True
                    outs = proc.drain_raw() + [item]  # sırayı koru: önce tamponda bekleyen ham metin
                for out in outs:
                    choices = out.get("choices") if isinstance(out, dict) else getattr(out, "choices", None)
                    if isinstance(choices, list) and not choices:
                        trailing.append(out)  # usage-only parça: tampon kalanından sonra gelsin
                    else:
                        for t in trailing:
                            yield t
                        trailing.clear()
                        yield out  # choices'lı parça, olay veya ham SSE (bytes/str) gecikmeden geçer
            for last in (proc.final_items() if not failed else []):
                yield last
            for t in trailing:
                yield t
        finally:
            if self.cfg.mask_logs:
                self._recent.put(request_data.get("litellm_call_id"), session)
            await self._safe_pop(sid)
            self._metrics_for(GuardrailEventHooks.post_call, "success_stream", time.monotonic() - t_stream, session,
                              {"unresolved": rev.stats.unresolved}, None)
            self._audit(
                request_data, user_api_key_dict, None, "success", session, (time.monotonic() - t_stream) * 1000,
                event="post_call_stream", unresolved=rev.stats.unresolved, replaced=rev.stats.replaced,
            )

    # ================================================================== UI / apply_guardrail
    async def apply_guardrail(self, inputs: Any, request_data: dict[str, Any], input_type: str, logging_obj: Any = None) -> Any:
        """Admin UI "guardrail test" / birleşik arayüz. Kendi değersiz kaydını üretir (records_own_guardrail_information);
        istisna metni LiteLLM'in guardrail_information kaydına düşmesin diye tüm hatalar sabit iletiye çevrilir."""
        t0 = time.monotonic()
        rd: dict[str, Any] = request_data if isinstance(request_data, dict) else {}
        texts = list((inputs or {}).get("texts") or [])
        out = dict(inputs or {})
        try:
            if input_type == "request":
                session = MaskSession(self.cfg)
                session.reserve_existing(t for t in texts if isinstance(t, str))
                analyses = [self.engine.analyze(t) if isinstance(t, str) else [] for t in texts]
                if self.cfg.dry_run:
                    would: Counter[str] = Counter()
                    for spans in analyses:
                        to_mask, blk, to_log = self.engine.actionable(spans)
                        session.count_log(to_log)
                        would.update(s.entity for s in to_mask + blk)
                    self._log(rd, "success", session, t0, GuardrailEventHooks.pre_call,
                              {"source": "apply_guardrail", "dry_run": True, "would_mask": dict(would)})
                    return inputs
                for spans in analyses:
                    _, blk, _ = self.engine.actionable(spans)
                    if blk:
                        self._log(rd, "guardrail_intervened", session, t0, GuardrailEventHooks.pre_call,
                                  {"blocked": blk[0].entity, "source": "apply_guardrail"})
                        raise HTTPException(status_code=400, detail={"error": "matriks_pii_blocked",
                                                                     "guardrail": self.guardrail_name, "entity": blk[0].entity})
                new = []
                for t, spans in zip(texts, analyses, strict=True):
                    to_mask, _, to_log = self.engine.actionable(spans)
                    session.count_log(to_log)
                    new.append(self.engine.mask(t, to_mask, session) if isinstance(t, str) else t)
                if session.map:
                    sid = f"{self.guardrail_name}:{uuid.uuid4().hex}"
                    await self.store.put(sid, session)
                    if rd:
                        self._set_session_id(rd, None, sid)
                out["texts"] = new
                status = "guardrail_intervened" if session.map or session.log_counts else "success"
                self._log(rd, status, session, t0, GuardrailEventHooks.pre_call, {"source": "apply_guardrail"})
                return out
            rsid = self._session_id(rd) if rd else None
            rsession = await self.store.pop(rsid) if rsid else None
            if rsession is None:
                if rd and self._token_present(rd):
                    self._session_missing(rd, None, "apply_guardrail_response")
                return out
            rev = self._reverter(rsession)
            out["texts"] = [rev.revert(t) if isinstance(t, str) else t for t in texts]
            self._log(rd, "success", rsession, t0, GuardrailEventHooks.post_call,
                      {"source": "apply_guardrail", "replaced": rev.stats.replaced, "unresolved": rev.stats.unresolved})
            return out
        except HTTPException:
            raise
        except Exception as e:
            _log_error("apply_guardrail", e)
            if input_type != "request" and self.cfg.revert_fail_mode == "open":
                return inputs
            # istek yönünde her zaman kapalı: maskelenmemiş metin dönmemeli
            raise HTTPException(status_code=500, detail={"error": "matriks_pii_guardrail_failure", "guardrail": self.guardrail_name}) from None

    # ================================================================== loglara giden kopyalar
    async def async_logging_hook(self, kwargs: dict[str, Any], result: Any, call_type: str) -> tuple[dict[str, Any], Any]:
        # analiz CPU işidir; olay döngüsünü (proxy'nin diğer istekleri) bloklamasın
        return await self._in_executor(self.logging_hook, kwargs, result, call_type)  # type: ignore[no-any-return]

    def logging_hook(self, kwargs: dict[str, Any], result: Any, call_type: str) -> tuple[dict[str, Any], Any]:
        """Loglanan KOPYAYI maskeler. Proxy istemciye dönen (geri çevrilmiş) yanıtı loglar: bu istek için
        bilinen değerler kesin olarak (eski yer tutucularıyla), kalanlar yeniden tespitle maskelenir.
        `logging_only` tek başına canlı isteği KORUMAZ."""
        try:
            known = self._recent.get(kwargs.get("litellm_call_id"))
            session = MaskSession(self.cfg)
            kw = copy.copy(kwargs)
            if isinstance(kw.get("messages"), list):
                msgs = copy.deepcopy(kw["messages"])
                commits: list[Callable[[], None]] = []
                for ref in list(iter_request_texts({"messages": msgs}, call_type, self.cfg, commits)):
                    self._mask_ref(ref, session, known)
                for c in commits:
                    c()
                kw["messages"] = msgs
            for key in ("standard_logging_object",):
                slo = kw.get(key)
                if isinstance(slo, dict):
                    slo = dict(slo)
                    for k2 in ("response", "messages"):
                        if slo.get(k2) is not None:
                            slo[k2] = self._mask_leaves(copy.deepcopy(slo[k2]), session, known)
                    kw[key] = slo
            for key in ("served_output_texts", "response", "complete_streaming_response", "async_complete_streaming_response"):
                if kw.get(key) is not None:
                    kw[key] = self._mask_leaves(copy.deepcopy(kw[key]), session, known)
            res = result
            if result is not None and (hasattr(result, "choices") or isinstance(result, dict)):
                res = result.model_copy(deep=True) if hasattr(result, "model_copy") else copy.deepcopy(result)
                commits2: list[Callable[[], None]] = []
                for ref in list(iter_response_texts(res, self.cfg, commits2)):
                    self._mask_ref(ref, session, known)
                for c in commits2:
                    c()
            return kw, res
        except Exception as e:
            _log_error("logging_hook", e)
            return kwargs, result

    def _mask_text(self, t: str, session: MaskSession, known: list[tuple[str, str]]) -> str:
        for value, ph in known:  # bu isteğin bilinen değerleri: kesin değiştirme (bağlam gerektirmez)
            if value in t:
                t = t.replace(value, ph)
        to_mask, blk, _ = self.engine.actionable(self.engine.analyze(t))
        return self.engine.mask(t, to_mask + blk, session, count=False) if (to_mask or blk) else t

    def _mask_ref(self, ref: TextRef, session: MaskSession, known: list[tuple[str, str]]) -> None:
        t = ref.get()
        if isinstance(t, str) and t:
            ref.set(self._mask_text(t, session, known))

    def _mask_leaves(self, node: Any, session: MaskSession, known: list[tuple[str, str]]) -> Any:
        if isinstance(node, str):
            return self._mask_text(node, session, known) if node else node
        if isinstance(node, dict):
            return {k: self._mask_leaves(v, session, known) for k, v in node.items()}
        if isinstance(node, list | tuple):
            return type(node)(self._mask_leaves(v, session, known) for v in node)
        if hasattr(node, "model_dump"):
            return self._mask_leaves(node.model_dump(), session, known)
        return node


def _mark_applied(data: dict[str, Any], name: str | None) -> None:
    """`x-litellm-applied-guardrails` yanıt başlığı için (sürüme göre yardımcı yoksa sessizce geç)."""
    try:
        from litellm.proxy.common_utils.callback_utils import add_guardrail_to_applied_guardrails_header

        add_guardrail_to_applied_guardrails_header(request_data=data, guardrail_name=name)
    except Exception:  # pragma: no cover
        pass


def _inject_instruction(data: dict[str, Any], call_type: str) -> None:
    """Yer tutucu talimatı system mesajının SONUNA eklenir (prompt-cache öneki bozulmasın)."""
    if call_type in GEMINI_CALL_TYPES:
        cfg_holder = data.get("config") if isinstance(data.get("config"), dict) else None
        for holder, key in ((data, "systemInstruction"), (data, "system_instruction"),
                            (cfg_holder, "system_instruction"), (cfg_holder, "systemInstruction")):
            if holder is None or key not in holder:
                continue
            cur = holder[key]
            if isinstance(cur, str):
                holder[key] = cur + "\n\n" + PLACEHOLDER_INSTRUCTION
            elif isinstance(cur, dict) and isinstance(cur.get("parts"), list):
                cur["parts"].append({"text": PLACEHOLDER_INSTRUCTION})
            elif isinstance(cur, list):
                cur.append({"text": PLACEHOLDER_INSTRUCTION})
            return
        data["systemInstruction"] = {"parts": [{"text": PLACEHOLDER_INSTRUCTION}]}
        return
    if call_type in ("responses", "aresponses"):
        cur = data.get("instructions")
        data["instructions"] = (cur + "\n\n" if isinstance(cur, str) and cur else "") + PLACEHOLDER_INSTRUCTION
        return
    if call_type in ("anthropic_messages", "aanthropic_messages"):
        cur = data.get("system")
        if isinstance(cur, str) or cur is None:
            data["system"] = ((cur + "\n\n") if cur else "") + PLACEHOLDER_INSTRUCTION
        elif isinstance(cur, list):
            cur.append({"type": "text", "text": PLACEHOLDER_INSTRUCTION})
        return
    msgs = data.get("messages")
    if not isinstance(msgs, list):
        return
    for m in msgs:
        if isinstance(m, dict) and m.get("role") in ("system", "developer"):
            c = m.get("content")
            if isinstance(c, str):
                m["content"] = c + "\n\n" + PLACEHOLDER_INSTRUCTION
            elif isinstance(c, list):
                c.append({"type": "text", "text": PLACEHOLDER_INSTRUCTION})
            return
    msgs.insert(0, {"role": "system", "content": PLACEHOLDER_INSTRUCTION})


_CUSTOM_GUARDRAIL_KWARGS = frozenset(
    {
        "supported_event_hooks", "mask_request_content", "mask_response_content", "violation_message_template",
        "end_session_after_n_fails", "on_violation", "realtime_violation_message", "on_sensitive_data",
        "sensitive_data_route_to_model", "sticky_session_routing", "run_in_parallel", "scan_raw_request",
        "only_scan_new_messages", "timeout", "guardrail_id", "fail_on_error", "api_key", "api_base",
    }
)
