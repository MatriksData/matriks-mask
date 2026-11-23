"""Yapılandırma şeması, profiller ve LiteLLM kwargs dönüştürücüsü."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .text_utils import ascii_fold, tr_upper

Action = Literal["MASK", "BLOCK", "LOG", "IGNORE"]

# ---------------------------------------------------------------- varlıklar
CORE_ENTITIES: tuple[str, ...] = (
    "TCKN",
    "VKN",
    "IBAN",
    "KART",
    "KART_CVV",
    "KART_SKT",
    "TELEFON",
    "EPOSTA",
    "IP",
    "MAC",
    "PLAKA",
    "PASAPORT",
    "KIMLIK_SERI",
    "NUFUS_KAYIT",
    "EHLIYET",
    "SGK",
    "MERSIS",
    "ETTN",
    "HESAP_NO",
    "MUSTERI_NO",
    "IMEI",
    "SASI_NO",
    "KRIPTO_CUZDAN",
    "SIFRE",
    "DOGUM_TARIHI",
    "DOGUM_YERI",
    "ADRES",
    "KISI",
    "GIZLI_ANAHTAR",
    "KONUM",
    "POLICE_NO",
    "TICARET_SICIL",
    "MKK_SICIL",
    "ADRES_KODU",
)

SPECIAL_ENTITIES: tuple[str, ...] = (
    "OZEL_SAGLIK",
    "OZEL_GENETIK",
    "OZEL_BIYOMETRIK",
    "OZEL_CINSEL",
    "OZEL_CEZA",
    "OZEL_CEZA_GENIS",
    "OZEL_DIN",
    "OZEL_ETNIK",
    "OZEL_SIYASI",
    "OZEL_FELSEFI",
    "OZEL_KIYAFET",
    "OZEL_UYELIK",
)

ALL_BUILTIN_ENTITIES: tuple[str, ...] = CORE_ENTITIES + SPECIAL_ENTITIES

PRIORITY: dict[str, int] = {
    "IBAN": 100,
    "KART": 95,
    "TCKN": 90,
    "MERSIS": 97,
    "ETTN": 88,
    "IMEI": 88,
    "SASI_NO": 88,
    "KRIPTO_CUZDAN": 88,
    "VKN": 85,
    "TELEFON": 80,
    "EPOSTA": 80,
    "IP": 75,
    "MAC": 75,
    "PLAKA": 70,
    "PASAPORT": 70,
    "KIMLIK_SERI": 70,
    "EHLIYET": 70,
    "SGK": 70,
    "HESAP_NO": 70,
    "MUSTERI_NO": 70,
    "NUFUS_KAYIT": 70,
    "KART_CVV": 70,
    "KART_SKT": 70,
    "SIFRE": 72,
    "DOGUM_TARIHI": 60,
    "DOGUM_YERI": 60,
    "ADRES": 55,
    "KISI": 50,
    "GIZLI_ANAHTAR": 101,
    "KONUM": 74,  # SIFRE (72) üstünde kalmalı: "harita pini 40.71, 29.01"
    "MKK_SICIL": 71,
    "POLICE_NO": 70,
    "TICARET_SICIL": 70,
    "ADRES_KODU": 70,
}
for _e in SPECIAL_ENTITIES:
    PRIORITY[_e] = 10

_PLACEHOLDER_FMT_RE = re.compile(r"^(?P<open>[^{}\s]{0,4})\{label\}(?P<sep>[_\-])\{n\}(?P<close>[^{}\s]{0,4})$")
_LABEL_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class EntityCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Action = "MASK"
    restore: bool = True
    min_score: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="before")
    @classmethod
    def _short(cls, v: Any) -> Any:
        if isinstance(v, str):
            return {"action": _norm_upper(v)}
        if isinstance(v, dict) and isinstance(v.get("action"), str):
            v = dict(v)
            v["action"] = _norm_upper(v["action"])
        return v


class NameDetectionCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lexicon: bool = True
    propagate_names: bool = True
    # aynı kişinin kısa anılışı ("Ayşe Demir" → "Ayşe'ye", "Demir Hanım", "A. Demir") metinde/oturumda tek bir tam
    # adla eşleşiyorsa o adın yer tutucusunu alır; birden çok aday varsa (iki "Ayşe") ayrı yer tutucu kalır.
    # Bedeli: yanıtta kısa anılış da tam adla geri çevrilir ("Ayşe'ye" → "Ayşe Demir'e"); bu yüzden varsayılan kapalı.
    coreference: bool = False
    ner_backend: Literal["none", "hf"] = "none"
    ner_model: str = "akdeniz27/bert-base-turkish-cased-ner"
    ner_label_map: dict[str, str] = Field(default_factory=lambda: {"PER": "KISI", "B-PER": "KISI", "I-PER": "KISI"})
    ner_min_score: float = Field(default=0.85, ge=0, le=1)
    ner_device: int = -1
    # üretim dayanıklılığı: model 512 token üstünde hata verir → cümle sınırında parçalara bölünür
    ner_max_chars: int = Field(default=1200, ge=200, le=4000)
    ner_timeout_seconds: float = Field(default=5.0, ge=0.1, le=60)
    # closed: NER hatası/zaman aşımı → istek reddedilir (fail_mode ile); lexicon: sözlük sonuçlarıyla devam
    ner_fail_mode: Literal["closed", "lexicon"] = "closed"
    ner_preload: bool = True  # model guardrail kurulurken yüklenir (ilk istek gecikmez; yüklenemezse proxy başlamaz)
    ner_max_concurrency: int = Field(default=1, ge=1, le=8)


class PhoneCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mask_444: bool = False
    mask_800: bool = False


class StreamingCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    holdback_max_chars: int = Field(default=48, ge=16, le=512)


class RevertFuzzyCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    max_l_dist: int = 1


class RevertCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fuzzy: RevertFuzzyCfg = Field(default_factory=RevertFuzzyCfg)
    restore_last4: bool = True


class SessionStoreCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backend: Literal["memory", "redis"] = "memory"
    ttl_seconds: int = Field(default=3600, ge=60)
    max_sessions: int = Field(default=10_000, ge=1, le=1_000_000)
    redis_url: str | None = None
    # AES-GCM anahtarı (32 bayt, base64). Verilmezse MATRIKS_PII_STORE_KEY ortam değişkeni. Yalnız redis.
    encryption_key: str | None = None
    # rotasyon: eski anahtar (yalnız çözme). Verilmezse MATRIKS_PII_STORE_KEY_PREVIOUS
    encryption_key_previous: str | None = None
    redis_timeout_seconds: float = Field(default=2.0, ge=0.05, le=30)
    refresh_ttl: bool = True  # get'te TTL yenile (uzun akışlar için)
    require_tls: bool = False  # true ise redis:// (TLS'siz) URL başlangıçta reddedilir


class CacheCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_entries: int = Field(default=2048, ge=0)
    # önbellek span'leri (orijinal değerlerle) bellekte tutar → kısa TTL; max_entries: 0 kapatır
    ttl_seconds: int = Field(default=300, ge=0)
    max_total_spans: int = Field(default=200_000, ge=0)


class AuditCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    path: str | None = None


class DenyItem(BaseModel):
    """Her zaman maskelenecek düz ifade (regex değil; sözcük sınırlı, Türkçe büyük/küçük harf duyarsız)."""

    model_config = ConfigDict(extra="forbid")
    phrase: str = Field(min_length=2, max_length=200)
    entity: str
    action: Action = "MASK"
    restore: bool = True

    @field_validator("entity")
    @classmethod
    def _ent(cls, v: str) -> str:
        v = _norm_upper(v)
        if not _LABEL_RE.match(v):
            raise ValueError(f"deny_list entity ASCII büyük harf olmalı: {v!r}")
        return v


# allow_list'e konamayacak kimlik biçimleri (kontrol basamaklı/kesin tanımlayıcılar): yanlışlıkla gerçek veri muafiyeti
_ALLOW_FORBIDDEN = re.compile(r"[0-9][0-9\s\-.]{8,}[0-9]|sk-|ghp_|AKIA|eyJ", re.IGNORECASE)
ALLOW_PROTECTED = frozenset({"TCKN", "VKN", "IBAN", "KART", "KART_CVV", "GIZLI_ANAHTAR", "SIFRE"})


class ExtraRegex(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity: str
    pattern: str
    score: float = Field(default=0.8, ge=0, le=1)
    context: str | None = None
    context_window: int = Field(default=60, ge=1, le=500)
    ignore_case: bool = True
    action: Action = "MASK"
    restore: bool = True
    # yalnız bu yakalama grubu maskelenir ("AccountId=(\d+)" → yalnız sayı); verilmezse eşleşmenin tamamı
    group: int | str | None = None

    @field_validator("group", mode="before")
    @classmethod
    def _grp_type(cls, v: object) -> object:
        if isinstance(v, bool):  # YAML "group: true" sessizce 1 olmasın
            raise ValueError("extra_regex group sayı ya da grup adı olmalı")
        return v

    @model_validator(mode="after")
    def _grp(self) -> ExtraRegex:
        if self.group is not None:
            rx = re.compile(self.pattern)
            if isinstance(self.group, int):
                if not 0 <= self.group <= rx.groups:
                    raise ValueError(f"extra_regex group {self.group}: desende {rx.groups} yakalama grubu var")
            elif self.group not in rx.groupindex:
                raise ValueError(f"extra_regex group {self.group!r}: desende bu adlı grup yok")
        return self

    @field_validator("entity")
    @classmethod
    def _ent(cls, v: str) -> str:
        v = _norm_upper(v)
        if not _LABEL_RE.match(v):
            raise ValueError(f"extra_regex entity ASCII büyük harf olmalı: {v!r}")
        return v

    @field_validator("pattern")
    @classmethod
    def _pat(cls, v: str) -> str:
        try:
            re.compile(v)
        except re.error as e:
            raise ValueError(f"extra_regex pattern geçersiz: {e.msg}") from None
        return v


def _norm_upper(x: Any) -> str:
    return ascii_fold(tr_upper(str(x))).strip()


def _default_entities() -> dict[str, EntityCfg]:
    ents = {e: EntityCfg() for e in ALL_BUILTIN_ENTITIES}
    ents["KART"] = EntityCfg(action="MASK", restore=False)
    ents["KART_CVV"] = EntityCfg(action="MASK", restore=False)
    ents["GIZLI_ANAHTAR"] = EntityCfg(action="MASK", restore=False)  # yanıtta "****"
    ents["SIFRE"] = EntityCfg(action="MASK", restore=False)  # LLM görmez, istek düşmez; yanıtta "****" (strict: BLOCK)
    ents["MAC"] = EntityCfg(action="IGNORE")
    # şirket ticaret sicil numarası çoğunlukla tüzel kişiye aittir; şahıs işletmeleri için açılabilir
    ents["TICARET_SICIL"] = EntityCfg(action="IGNORE")
    ents["KRIPTO_CUZDAN"] = EntityCfg(action="IGNORE")
    for e in ("OZEL_CEZA_GENIS", "OZEL_DIN", "OZEL_ETNIK", "OZEL_SIYASI", "OZEL_FELSEFI", "OZEL_KIYAFET", "OZEL_UYELIK"):
        ents[e] = EntityCfg(action="LOG")
    return ents


DEFAULT_LABEL_ALIASES: dict[str, str] = {
    "PERSON": "KISI", "NAME": "KISI", "ISIM": "KISI", "PHONE": "TELEFON", "EMAIL": "EPOSTA", "ID": "TCKN",
    "ADDRESS": "ADRES", "LOCATION": "KONUM",
}

DEFAULT_MIN_SCORES: dict[str, float] = {"KISI": 0.5, "TELEFON": 0.6, "PLAKA": 0.6, "ADRES": 0.6}


class TurkishPIIConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: Literal["strict", "balanced", "minimal"] = "balanced"
    entities: dict[str, EntityCfg] = Field(default_factory=_default_entities)
    placeholder_format: str = "<{label}_{n}>"
    placeholder_id: Literal["sequence", "hmac"] = "sequence"
    hash_secret: str | None = None
    # oturum jetonu HMAC sırrı (≥32 karakter). Verilmezse MATRIKS_PII_SESSION_SECRET; o da yoksa süreç başına rastgele
    # (yalnız memory deposunda geçerli). redis deposunda (çoklu worker/replika) zorunlu ve tüm örneklerde aynı olmalı.
    session_secret: str | None = None
    # rotasyon: eski sır (yalnız doğrulama). Verilmezse MATRIKS_PII_SESSION_SECRET_PREVIOUS
    session_secret_previous: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    placeholder_instruction: Literal["none", "system_suffix"] = "none"
    revert_response: bool = True
    revert_fail_mode: Literal["open", "closed"] = "closed"
    unresolved_placeholder: Literal["keep", "strip"] = "keep"
    truncated_placeholder: Literal["strip", "keep"] = "strip"
    revert: RevertCfg = Field(default_factory=RevertCfg)
    response_leak_report: bool = False
    scan_response: bool = False  # yanıtta LLM'in ürettiği yeni PII'yi maskele (geri çevrilmez; akışta uygulanmaz)
    tool_restore_policy: Literal["all", "none"] = "all"
    mask_system_messages: bool = True
    mask_assistant_messages: bool = True
    mask_tool_messages: bool = True
    mask_embeddings: bool = True
    name_detection: NameDetectionCfg = Field(default_factory=NameDetectionCfg)
    phone: PhoneCfg = Field(default_factory=PhoneCfg)
    tckn_require_checksum: bool = True
    # strict: kontrol basamağı tutmayan TCKN/VKN/IBAN/KART/IMEI tespit edilmez (yüksek kesinlik)
    # context: hemen önünde açık etiket varsa ("tc", "iban", "kart no", "vergi no", "imei") hatalı yazılmış değer de maskelenir
    checksum_policy: Literal["strict", "context"] = "strict"
    context_window: int = Field(default=60, ge=1, le=500)
    min_scores: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_MIN_SCORES))
    extra_regex: list[ExtraRegex] = Field(default_factory=list)
    # asla maskelenmeyecek ifadeler (ör. kamuya açık yönetici adı, marka); span'i tamamen kapsayan geçiş → atılır.
    # TCKN/VKN/IBAN/KART/CVV/GIZLI_ANAHTAR/SIFRE için uygulanmaz.
    allow_list: list[str] = Field(default_factory=list)
    # her zaman maskelenecek düz ifadeler (proje kod adı, iç sistem adı...) → kendi varlık etiketiyle
    deny_list: list[DenyItem] = Field(default_factory=list)
    streaming: StreamingCfg = Field(default_factory=StreamingCfg)
    session_store: SessionStoreCfg = Field(default_factory=SessionStoreCfg)
    analysis_cache: CacheCfg = Field(default_factory=CacheCfg)
    executor_threshold_chars: int = Field(default=8192, ge=0)
    max_text_chars: int = Field(default=2_000_000, ge=1_024, le=10_000_000)
    max_request_chars: int = Field(default=4_000_000, ge=1_024, le=50_000_000)  # istekteki tüm metinlerin toplamı
    analysis_threads: int = Field(default=4, ge=1, le=64)  # analiz/log maskeleme için ayrılmış iş parçacığı havuzu
    mask_logs: bool = True  # proxy loglarına giden (geri çevrilmiş) yanıtı ve isteği yeniden maskeler
    unsupported_call_types: Literal["mask", "pass", "block"] = "mask"  # rerank, görsel, ses, MCP...
    store_map_in_metadata: bool = False
    suffix_repair: bool = True
    # yanıt içeriğindeki JSON metin ayrıştırılıp yeniden yazılmaz (girinti/sıra korunur); değerler JSON-kaçışlı yazılır
    response_json_preserve_format: bool = True
    # apostrofsuz bitişik eki de onar ("<KISI_1>lar" → "Ayşe Demirler"); ipucusuz metin değişikliği → varsayılan kapalı
    suffix_repair_attached: bool = False
    # LLM'in çevirdiği etiketler yalnız parantezli biçimde gerçek etikete eşlenir ("<PERSON_1>" → <KISI_1>)
    label_aliases: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_LABEL_ALIASES))
    fail_mode: Literal["open", "closed"] = "closed"
    # gölge mod: hiçbir metin değiştirilmez/engellenmez, yalnız "maskelenecekti" sayımları loglanır (kademeli yayın)
    dry_run: bool = False
    # Gemini native /v1beta/models/...:generateContent (+ stream) rotası: tam maskele + geri çevir. false → "desteklenmeyen
    # çağrı" yolu (yalnız maskeleme, yanıt geri çevrilmez)
    gemini_native: bool = True
    metrics_enabled: bool = True  # prometheus_client kuruluysa değersiz sayaçlar (matriks_pii_*)
    audit_log: AuditCfg = Field(default_factory=AuditCfg)

    # ------------------------------------------------------------ doğrulama
    @model_validator(mode="before")
    @classmethod
    def _secret_types(cls, v: Any) -> Any:
        """Sır alanlarında tip hatası pydantic iletisinde DEĞERİ göstermesin (başlangıç logu)."""
        if not isinstance(v, dict):
            return v
        for key in _SECRET_FIELDS:
            if v.get(key) is not None and not isinstance(v.get(key), str):
                raise ValueError(f"{key} metin olmalı")
        ss = v.get("session_store")
        if isinstance(ss, dict):
            for key in _STORE_SECRET_FIELDS:
                if ss.get(key) is not None and not isinstance(ss.get(key), str):
                    raise ValueError(f"session_store.{key} metin olmalı")
        return v

    @field_validator("allow_list")
    @classmethod
    def _allow(cls, v: list[str]) -> list[str]:
        out = []
        for x in v:
            x = str(x).strip()
            if len(x) < 2:
                raise ValueError("allow_list girdisi en az 2 karakter olmalı")
            if _ALLOW_FORBIDDEN.search(x):
                raise ValueError("allow_list kimlik numarası/anahtar benzeri değer içeremez")
            out.append(x)
        return out

    @field_validator("min_scores")
    @classmethod
    def _min_scores(cls, v: dict[str, float]) -> dict[str, float]:
        for k, x in v.items():
            if not 0 <= x <= 1:
                raise ValueError(f"min_scores[{k}] 0..1 aralığında olmalı")
        return v

    @field_validator("placeholder_format")
    @classmethod
    def _fmt(cls, v: str) -> str:
        if not _PLACEHOLDER_FMT_RE.match(v):
            raise ValueError(
                "placeholder_format '{label}' ve '{n}' içermeli, aralarında '_' veya '-' olmalı, "
                "açılış/kapanış en fazla 4 karakter: ör. '<{label}_{n}>'"
            )
        return v

    @field_validator("labels")
    @classmethod
    def _labels(cls, v: dict[str, str]) -> dict[str, str]:
        out = {}
        for k, lab in v.items():
            lab_n = _norm_upper(lab)
            if not _LABEL_RE.match(lab_n):
                raise ValueError(f"etiket ASCII büyük harf olmalı: {lab!r}")
            out[_norm_upper(k)] = lab_n
        return out

    @model_validator(mode="after")
    def _post(self) -> TurkishPIIConfig:
        if self.placeholder_id == "hmac" and not self.hash_secret:
            raise ValueError("placeholder_id=hmac için hash_secret zorunlu")
        secret = self.effective_session_secret
        if secret is not None and len(secret) < 32:
            raise ValueError("session_secret / MATRIKS_PII_SESSION_SECRET en az 32 karakter olmalı")
        prev = self.effective_session_secret_previous
        if prev is not None and len(prev) < 32:
            raise ValueError("session_secret_previous / MATRIKS_PII_SESSION_SECRET_PREVIOUS en az 32 karakter olmalı")
        url = self.session_store.redis_url
        if self.session_store.require_tls and url and not url.startswith("rediss://"):
            raise ValueError("session_store.require_tls: redis_url 'rediss://' (TLS) olmalı")
        if self.session_store.backend == "redis":
            if secret is None:
                raise ValueError(
                    "redis session_store (çoklu worker/replika) için session_secret zorunlu: tüm örneklerde aynı "
                    "MATRIKS_PII_SESSION_SECRET tanımlayın (python -m matriks_pii.keys)"
                )
            if not (self.session_store.encryption_key or os.environ.get("MATRIKS_PII_STORE_KEY")):
                raise ValueError(
                    "redis session_store için session_store.encryption_key / MATRIKS_PII_STORE_KEY zorunlu (python -m matriks_pii.keys)"
                )
        if self.store_map_in_metadata and os.environ.get("MATRIKS_PII_ALLOW_DEBUG_MAP") != "1":
            raise ValueError(
                "store_map_in_metadata yalnızca MATRIKS_PII_ALLOW_DEBUG_MAP=1 ile açılabilir (üretimde yasak; ham veri aktarımıdır)"
            )
        if self.max_request_chars < self.max_text_chars:
            raise ValueError("max_request_chars, max_text_chars'tan küçük olamaz")
        custom = {x.entity for x in self.extra_regex} | {d.entity for d in getattr(self, "deny_list", [])}
        unknown = sorted(e for e in self.entities if e not in ALL_BUILTIN_ENTITIES and e not in custom)
        if unknown:
            raise ValueError(f"bilinmeyen varlık: {', '.join(unknown)} (yazım hatası? extra_regex/deny_list ile tanımlayın)")
        for x in self.extra_regex:
            if x.entity not in self.entities:
                self.entities[x.entity] = EntityCfg(action=x.action, restore=x.restore)
        for d in self.deny_list:
            if d.entity not in self.entities:
                self.entities[d.entity] = EntityCfg(action=d.action, restore=d.restore)
        for e in list(self.entities):
            if e not in self.labels:
                self.labels[e] = e
        longest = max(len(self.format_placeholder(lab, "x" * 12)) for lab in self.labels.values())
        if self.streaming.holdback_max_chars < longest + 8:
            raise ValueError(
                f"streaming.holdback_max_chars ({self.streaming.holdback_max_chars}) en uzun yer tutucudan ({longest}) "
                "en az 8 fazla olmalı; aksi hâlde akışta bölünen yer tutucu geri çevrilmez"
            )
        return self

    # ------------------------------------------------------------ yardımcılar
    def action(self, entity: str) -> str:
        ec = self.entities.get(entity)
        return ec.action if ec else "IGNORE"

    def restore(self, entity: str) -> bool:
        ec = self.entities.get(entity)
        return True if ec is None else ec.restore

    def min_score(self, entity: str) -> float:
        ec = self.entities.get(entity)
        if ec is not None and ec.min_score is not None:
            return ec.min_score
        return self.min_scores.get(entity, 0.5)

    def label(self, entity: str) -> str:
        return self.labels.get(entity, entity)

    @property
    def label_to_entity(self) -> dict[str, str]:
        return {v: k for k, v in self.labels.items()}

    @property
    def fmt_parts(self) -> tuple[str, str, str]:
        m = _PLACEHOLDER_FMT_RE.match(self.placeholder_format)
        assert m
        return m["open"], m["sep"], m["close"]

    def format_placeholder(self, label: str, n: int | str) -> str:
        return self.placeholder_format.replace("{label}", label).replace("{n}", str(n))

    @property
    def version_hash(self) -> str:
        # sırlar hash'e girmez: denetim izindeki config_hash sırdan türetilmiş bilgi taşımasın
        dumped = self.model_dump_json(exclude={
            "hash_secret": True, "session_secret": True, "session_secret_previous": True,
            "session_store": {"encryption_key", "encryption_key_previous", "redis_url"},
        })
        return hashlib.sha256(dumped.encode("utf-8")).hexdigest()[:16]

    @property
    def effective_session_secret(self) -> str | None:
        return self.session_secret or os.environ.get("MATRIKS_PII_SESSION_SECRET") or None

    @property
    def effective_session_secret_previous(self) -> str | None:
        return self.session_secret_previous or os.environ.get("MATRIKS_PII_SESSION_SECRET_PREVIOUS") or None

    # ------------------------------------------------------------ kurulum
    @classmethod
    def build(cls, overrides: dict[str, Any] | None = None) -> TurkishPIIConfig:
        """Profil + açık anahtarlar. Açık anahtarlar profili ezer; entities derin birleştirilir."""
        raw = copy.deepcopy(overrides or {})
        if os.environ.get("MATRIKS_PII_IGNORE_UNKNOWN_KEYS") == "1":  # acil geri alma: yeni sürüm config'i eski sürümde
            raw = _prune_unknown(raw, cls)
        raw = _normalize_raw(raw)
        profile = raw.get("profile", "balanced")
        try:
            base = cls(profile=profile).model_dump()
            _apply_profile(base, profile)
            merged = _deep_merge(base, raw)
            return cls.model_validate(merged)
        except ValidationError as e:
            # pydantic iletisi girdi değerini (sırlar dâhil) gösterir → yalnız konum + ileti (değersiz)
            msgs = "; ".join(f"{'.'.join(str(x) for x in err['loc']) or '<config>'}: {err['msg']}"
                             for err in e.errors(include_input=False, include_url=False))
            raise ValueError(f"matriks_pii yapılandırma hatası: {msgs}") from None

    @classmethod
    def from_kwargs(cls, kwargs: dict[str, Any]) -> tuple[TurkishPIIConfig, dict[str, Any]]:
        known = set(cls.model_fields) | {"special_categories"}
        _warn_literal_secrets(kwargs)
        mine = {k: _resolve_env(v) for k, v in kwargs.items() if k in known and v is not None}
        rest = {k: v for k, v in kwargs.items() if k not in known}
        if rest.get("fail_on_error") is False and "fail_mode" not in mine:
            mine["fail_mode"] = "open"
        return cls.build(mine), rest


_SECRET_FIELDS = ("session_secret", "session_secret_previous", "hash_secret")
_STORE_SECRET_FIELDS = ("encryption_key", "encryption_key_previous", "redis_url")
_log = logging.getLogger("matriks_pii")


def _warn_literal_secrets(kwargs: dict[str, Any]) -> None:
    """Sırlar config'de düz metin verilmişse uyar (UI/DB/config dökümlerinde görünebilir)."""
    found: list[str] = []
    for key in _SECRET_FIELDS:
        v = kwargs.get(key)
        if isinstance(v, str) and v and not v.startswith("os.environ/"):
            found.append(key)
    ss = kwargs.get("session_store")
    if isinstance(ss, dict):
        for key in _STORE_SECRET_FIELDS:
            v = ss.get(key)
            if not isinstance(v, str) or not v or v.startswith("os.environ/"):
                continue
            if key == "redis_url" and "@" not in v:
                continue  # parolasız URL sır değildir
            found.append(f"session_store.{key}")
    for name in found:
        msg = f"matriks_pii: {name} düz metin verilmiş; 'os.environ/AD' biçimini kullanın"
        if os.environ.get("MATRIKS_PII_STRICT_SECRETS") == "1":
            raise ValueError(msg)
        _log.warning(msg)


def _prune_unknown(raw: dict[str, Any], model: type[BaseModel]) -> dict[str, Any]:
    """Modelde olmayan anahtarları (uyarıyla) at; iç içe modellere özyinelemeli iner."""
    out: dict[str, Any] = {}
    for k, v in raw.items():
        field = model.model_fields.get(k)
        if field is None and not (model is TurkishPIIConfig and k == "special_categories"):
            _log.warning("matriks_pii: tanınmayan yapılandırma anahtarı yok sayıldı: %s", k)
            continue
        ann = field.annotation if field is not None else None
        if isinstance(v, dict) and isinstance(ann, type) and issubclass(ann, BaseModel):
            v = _prune_unknown(v, ann)
        out[k] = v
    return out


def _resolve_env(v: Any) -> Any:
    """LiteLLM geleneği: "os.environ/AD" → ortam değişkeninin değeri (iç içe yapılarda da)."""
    if isinstance(v, str) and v.startswith("os.environ/"):
        name = v.split("/", 1)[1]
        val = os.environ.get(name)
        if val is None:
            raise ValueError(f"ortam değişkeni tanımlı değil: {name}")
        return val
    if isinstance(v, dict):
        return {k: _resolve_env(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_resolve_env(x) for x in v]
    return v


def _normalize_raw(raw: dict[str, Any]) -> dict[str, Any]:
    ents: dict[str, Any] = {}
    for src in ("entities", "special_categories"):
        for k, v in (raw.pop(src, None) or {}).items():
            ents[_norm_upper(k)] = v
    if ents:
        raw["entities"] = ents
    return raw


def _apply_profile(base: dict[str, Any], profile: str) -> None:
    ents = base["entities"]
    if profile == "strict":
        for e in SPECIAL_ENTITIES:
            ents[e]["action"] = "MASK"
        for e in ("KRIPTO_CUZDAN", "MAC", "TICARET_SICIL"):
            ents[e]["action"] = "MASK"
        ents["GIZLI_ANAHTAR"]["action"] = "BLOCK"
        ents["SIFRE"]["action"] = "BLOCK"
        base["response_leak_report"] = True
        base["scan_response"] = True
        base["checksum_policy"] = "context"
        base["placeholder_instruction"] = "system_suffix"
        base["phone"]["mask_444"] = True
    elif profile == "minimal":
        keep = {"TCKN", "VKN", "IBAN", "KART", "TELEFON", "EPOSTA", "KISI", "GIZLI_ANAHTAR"}
        for e in CORE_ENTITIES:
            if e not in keep:
                ents[e]["action"] = "IGNORE"
        for e in SPECIAL_ENTITIES:
            ents[e]["action"] = "LOG"


# kullanıcı verince varsayılanla birleştirilmez, tamamen değiştirilir (ör. label_aliases: {} → takma ad yok)
_REPLACE_KEYS = frozenset({"label_aliases"})


def _deep_merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out = dict(a)
    for k, v in b.items():
        if k == "entities" and isinstance(v, dict):
            ents = dict(out.get("entities", {}))
            for ek, ev in v.items():
                cur = dict(ents.get(ek, {"action": "MASK", "restore": True, "min_score": None}))
                if isinstance(ev, str):
                    cur["action"] = _norm_upper(ev)
                elif isinstance(ev, dict):
                    cur.update(ev)
                    if isinstance(cur.get("action"), str):
                        cur["action"] = _norm_upper(cur["action"])
                ents[ek] = cur
            out["entities"] = ents
        elif isinstance(v, dict) and isinstance(out.get(k), dict) and k not in _REPLACE_KEYS:
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def config_hash_for(overrides: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(overrides, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
