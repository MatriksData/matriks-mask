"""Analiz motoru: iki fazlı tanıma, çakışma çözümü, maskeleme."""

from __future__ import annotations

import bisect
import dataclasses
import hashlib
import re
import threading
import time
from collections import OrderedDict
from typing import TYPE_CHECKING

from .config import ALLOW_PROTECTED, PRIORITY, TurkishPIIConfig
from .recognizers import build_recognizers
from .recognizers.base import EXTENDED_SUFFIX, AnalysisContext, Span
from .text_utils import ascii_dashes, ascii_width, decode_for_scan, fold_key, fold_text, form_plus_spaces, trie_pattern

if TYPE_CHECKING:
    from .session import MaskSession


def _priority(entity: str) -> int:
    return PRIORITY.get(entity, 65)


_PERSON = "KISI"


def _alias_candidates(key: str, full_keys: set[str]) -> set[str]:
    """Tek ad/soyad ("ayse", "demir") veya baş harf + soyad ("a. demir") anahtarına uyan tam ad anahtarları."""
    toks = key.split()
    if len(toks) == 1:
        return {f for f in full_keys if toks[0] in (f.split()[0], f.split()[-1])}
    if len(toks) == 2 and toks[0].endswith(".") and len(toks[0]) >= 2:
        initial = fold_key(toks[0][:-1])
        return {f for f in full_keys if f.split()[-1] == toks[1] and f.startswith(initial)}
    return set()


def _is_full_name(key: str) -> bool:
    toks = key.split()
    return len(toks) >= 2 and not toks[0].endswith(".")


def link_person_mentions(spans: list[Span], session: MaskSession) -> tuple[list[Span], dict[str, Span]]:
    """Aynı kişinin kısa anılışlarını tam adın anahtarına bağlar (eş-gönderim).

    Aday tam adlar bu metindeki ve oturumdaki (önceki mesajlar) KISI anahtarlarıdır. Kısa anılış ancak tek bir
    tam adla eşleşirse bağlanır; belirsizlikte (iki "Ayşe") eski davranış (ayrı yer tutucu) korunur.
    Dönüş: (normalized'i güncellenmiş span listesi, tam ad anahtarı → bu metindeki ilk tam ad span'i).
    """
    firsts: dict[str, Span] = {}
    for s in spans:
        if s.entity == _PERSON and _is_full_name(s.normalized):
            firsts.setdefault(s.normalized, s)
    full_keys = set(firsts) | {k for e, k in session.by_key if e == _PERSON and _is_full_name(k)}
    if not full_keys:
        return spans, firsts
    out: list[Span] = []
    for s in spans:
        if s.entity == _PERSON and not _is_full_name(s.normalized):
            cands = _alias_candidates(s.normalized, full_keys)
            if len(cands) == 1:
                s = dataclasses.replace(s, normalized=next(iter(cands)))
        out.append(s)
    return out, firsts


def resolve_overlaps(spans: list[Span]) -> list[Span]:
    """Öncelik > skor > uzunluk > başlangıç sırasıyla kabul et; kabul edilenlerle kesişeni at."""
    ordered = sorted(spans, key=lambda s: (-_priority(s.entity), -s.score, -(s.end - s.start), s.start))
    return _greedy_disjoint(ordered)


def _merge_decoded(text: str, orig: list[Span], dec: list[Span], cfg: TurkishPIIConfig) -> list[Span]:
    """Orijinal tarama span'leri asla düşürülmez. Çözülmüş kopyadan gelen span orijinal bir span'le kesişirse ikisi
    birleşimi kapsayan tek span olur (öncelikli varlık etiketiyle); hiçbir parça açıkta kalmaz."""
    if not dec:
        return orig
    items = sorted([(s.start, s.end, 0, s) for s in orig] + [(s.start, s.end, 1, s) for s in dec], key=lambda x: (x[0], -x[1]))
    clusters: list[list[Span]] = []
    cur_end = -1
    for st, en, _src, sp in items:
        if clusters and st < cur_end:
            clusters[-1].append(sp)
            cur_end = max(cur_end, en)
        else:
            clusters.append([sp])
            cur_end = en
    out: list[Span] = []
    for c in clusters:
        if len(c) == 1:
            out.append(c[0])
            continue
        rank = {"BLOCK": 3, "MASK": 2, "LOG": 1}  # IGNORE (0) temsilci olursa birleşim maskelenmez
        rep = max(c, key=lambda x: (rank.get(cfg.action(x.entity), 0), _priority(x.entity), x.score, x.end - x.start))
        u_s, u_e = min(x.start for x in c), max(x.end for x in c)
        if (u_s, u_e) == (rep.start, rep.end):
            out.append(rep)
        else:
            out.append(dataclasses.replace(rep, start=u_s, end=u_e, text=text[u_s:u_e], normalized=text[u_s:u_e]))
    return out


def _drop_conflicting_extensions(spans: list[Span]) -> list[Span]:
    """Bitişik parçalarla uzatılmış kimlik adayı, başka bir varlığın adayına değiyorsa atılır; temel aday kalır
    ("hesap no 12345678/05 Kasım 1985" → uzatma "…/05" doğum tarihini yutmasın)."""
    ext = [s for s in spans if s.recognizer.endswith(EXTENDED_SUFFIX)]
    if not ext:
        return spans
    others = sorted((s for s in spans if not s.recognizer.endswith(EXTENDED_SUFFIX)), key=lambda s: s.start)
    starts = [s.start for s in others]
    max_len = max((s.end - s.start for s in others), default=0)
    drop: set[int] = set()
    for x in ext:
        lo = bisect.bisect_left(starts, x.start - max_len)
        hi = bisect.bisect_left(starts, x.end)
        if any(o.entity != x.entity and o.start < x.end and x.start < o.end for o in others[lo:hi]):
            drop.add(id(x))
    return [s for s in spans if id(s) not in drop]


def _dedupe_same_entity(spans: list[Span]) -> list[Span]:
    """Aynı entity'de kısmi kesişen adaylar (NER + sözlük): uzun olanı tut."""
    by_ent: dict[str, list[Span]] = {}
    out: list[Span] = []
    for s in spans:
        if s.recognizer.endswith(EXTENDED_SUFFIX):
            out.append(s)  # uzatılmış kimlik adayı temel adayı elemesin; seçim resolve_overlaps'ta (öncelikli komşu varsa temel kalır)
            continue
        by_ent.setdefault(s.entity, []).append(s)
    for _ent, lst in by_ent.items():
        lst.sort(key=lambda s: (-(s.end - s.start), -s.score))
        out.extend(_greedy_disjoint(lst))
    return out


def _greedy_disjoint(ordered: list[Span]) -> list[Span]:
    """Sıralı adaylardan kabul edilenlerle kesişmeyenleri seç (ayrık aralık listesi + bisect, O(n log n))."""
    starts: list[int] = []
    accepted: list[Span] = []
    for s in ordered:
        if s.end <= s.start:
            continue
        i = bisect.bisect_left(starts, s.start)
        if i > 0 and accepted[i - 1].end > s.start:
            continue
        if i < len(accepted) and accepted[i].start < s.end:
            continue
        starts.insert(i, s.start)
        accepted.insert(i, s)
    return accepted


MAX_CACHED_TEXT_CHARS = 65_536


class AnalysisCache:
    """Metin özeti → span listesi (LRU + TTL). Span'ler ORİJİNAL değerleri taşır: kısa TTL, giriş ve toplam span sınırı."""

    def __init__(self, max_entries: int, ttl_seconds: int, max_total_spans: int = 200_000) -> None:
        self.max = max_entries
        self.ttl = ttl_seconds
        self.max_spans = max_total_spans
        self.total_spans = 0
        self._d: OrderedDict[str, tuple[float, list[Span]]] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._d)

    def _drop(self, key: str) -> None:
        v = self._d.pop(key, None)
        if v is not None:
            self.total_spans -= len(v[1])

    def get(self, key: str) -> list[Span] | None:
        if self.max <= 0:
            return None
        with self._lock:
            v = self._d.get(key)
            if v is None:
                return None
            ts, spans = v
            if time.monotonic() - ts > self.ttl:
                self._drop(key)
                return None
            self._d.move_to_end(key)
            return spans

    def put(self, key: str, spans: list[Span]) -> None:
        if self.max <= 0 or len(spans) > self.max_spans:
            return
        with self._lock:
            self._drop(key)
            self._d[key] = (time.monotonic(), spans)
            self.total_spans += len(spans)
            while self._d and (len(self._d) > self.max or self.total_spans > self.max_spans):
                old, (_, sp) = self._d.popitem(last=False)
                self.total_spans -= len(sp)

    def clear(self) -> None:
        with self._lock:
            self._d.clear()
            self.total_spans = 0


class PIIEngine:
    def __init__(self, cfg: TurkishPIIConfig) -> None:
        self.cfg = cfg
        self.recognizers = build_recognizers(cfg)
        self.cache = AnalysisCache(cfg.analysis_cache.max_entries, cfg.analysis_cache.ttl_seconds,
                                   cfg.analysis_cache.max_total_spans)
        self._cfg_hash = cfg.version_hash
        allow = sorted({" ".join(fold_text(x).split()) for x in cfg.allow_list}, key=len, reverse=True)
        self._allow_rx = re.compile(rf"(?<![0-9a-z])(?:{trie_pattern(allow)})(?![0-9a-z])") if allow else None

    # ------------------------------------------------------------------ analiz
    def analyze(self, text: str) -> list[Span]:
        """Tüm (IGNORE dahil) eşik üstü span'ler; çakışmalar çözülmüş."""
        if not text:
            return []
        if len(text) > self.cfg.max_text_chars:
            raise ValueError("matriks_pii: metin max_text_chars sınırını aşıyor")
        key = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest() + self._cfg_hash
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        orig = text
        text = ascii_width(text)  # 1:1; tanıyıcılar ASCII kopyada eşleşir, span metni aşağıda orijinalden alınır
        spans, ctx = self._detect(text)
        extra: list[Span] = []
        for variant in (ascii_dashes(text), form_plus_spaces(text)):
            # Unicode tireli yazım ve form "+"'sı ek taramada (1:1); ana tarama span'leri düşmez (birleşim)
            if variant is not None:
                for s in self._detect(variant)[0]:
                    raw = text[s.start : s.end]
                    changed = raw != variant[s.start : s.end]
                    extra.append(dataclasses.replace(s, text=raw, normalized=raw if changed else s.normalized))
        dec = decode_for_scan(text)
        if dec is not None:
            extra += self._decoded_spans(text, *dec)
        if extra:
            spans = _merge_decoded(text, spans, extra, self.cfg)
        if self._allow_rx is not None:
            zones = [(m.start(), m.end()) for m in self._allow_rx.finditer(ctx.casefolded)]
            if zones:  # izin listesindeki ifadenin bir geçişi span'i tamamen kapsıyorsa at (kesin kimlikler hariç)
                spans = [s for s in spans if s.entity in ALLOW_PROTECTED or not any(a <= s.start and s.end <= b for a, b in zones)]
        if text is not orig:
            spans = [dataclasses.replace(s, text=orig[s.start : s.end]) for s in spans]
        if len(text) <= MAX_CACHED_TEXT_CHARS:  # büyük metinler önbelleğe girmez (bellek + tekrar olasılığı düşük)
            self.cache.put(key, spans)
        return spans

    def _detect(self, text: str) -> tuple[list[Span], AnalysisContext]:
        ctx = AnalysisContext.build(self.cfg, text)
        phase1: list[Span] = []
        for r in self.recognizers:
            if r.phase == 1:
                phase1.extend(r.find(text, ctx))
        phase1 = _drop_conflicting_extensions([s for s in phase1 if s.score >= self.cfg.min_score(s.entity)])
        ctx.prior_spans = resolve_overlaps(_dedupe_same_entity(phase1))
        phase2: list[Span] = []
        for r in self.recognizers:
            if r.phase == 2:
                phase2.extend(r.find(text, ctx))
        phase2 = [s for s in phase2 if s.score >= self.cfg.min_score(s.entity)]
        return resolve_overlaps(_drop_conflicting_extensions(ctx.prior_spans + phase2)), ctx

    def _decoded_spans(self, text: str, decoded: str, starts: list[int], ends: list[int]) -> list[Span]:
        """URL/HTML kodu çözülmüş kopyada bulunan span'ler, orijinal aralığa taşınmış olarak. Yalnız kodlu bir bölgeye
        dokunan ya da bağlam penceresi içinde kodlu bölge olan span'ler alınır ("hesap%20no=12345678"); kodlu bölgeden
        uzak tespitler zaten orijinal taramada vardır, davranış değişmesin diye alınmaz."""
        enc = [0] * (len(decoded) + 1)  # önek toplamı: çözülmüş karakter kodlu bir diziden mi geldi
        for i in range(len(decoded)):
            enc[i + 1] = enc[i] + (ends[i] - starts[i] != 1 or decoded[i] != text[starts[i]])
        if not enc[-1]:
            return []
        w = self.cfg.context_window
        out: list[Span] = []
        scan = ascii_width(decoded)  # 1:1: çözülmüş &nbsp; / %C2%A0 / &#8209; da boşluk/tire sayılsın
        scan = ascii_dashes(scan) or scan
        for s in self._detect(scan)[0]:
            lo, hi = max(0, s.start - w), min(len(decoded), s.end + w)
            if enc[hi] - enc[lo] == 0:
                continue
            o_s, o_e = starts[s.start], ends[s.end - 1]
            raw = text[o_s:o_e]
            # kodlu karaktere dokunan değer ayrı yer tutucu alır (düz yazımı kodlu biçimle geri çevrilmesin)
            touched = enc[s.end] - enc[s.start] > 0
            out.append(dataclasses.replace(s, start=o_s, end=o_e, text=raw, normalized=raw if touched else s.normalized))
        return out

    def analyze_with_prefix(self, text: str, prefix: str = "") -> list[Span]:
        """Bağlam önekiyle analiz (ör. JSON anahtarı "Adı soyadı: "); span'ler `text`'e göre döner."""
        if not prefix:
            return self.analyze(text)
        n = len(prefix)
        return [
            dataclasses.replace(s, start=s.start - n, end=s.end - n) for s in self.analyze(prefix + text) if s.start >= n
        ]

    def actionable(self, spans: list[Span]) -> tuple[list[Span], list[Span], list[Span]]:
        """(maskelenecek, engelleyecek, yalnız sayılacak)."""
        mask, block, log = [], [], []
        for s in spans:
            a = self.cfg.action(s.entity)
            if a == "MASK":
                mask.append(s)
            elif a == "BLOCK":
                block.append(s)
            elif a == "LOG":
                log.append(s)
        return mask, block, log

    def detected(self, text: str) -> list[Span]:
        """IGNORE aksiyonlu olanlar hariç tespitler (fixture sözleşmesi)."""
        return [s for s in self.analyze(text) if self.cfg.action(s.entity) != "IGNORE"]

    def mask(self, text: str, spans: list[Span], session: MaskSession, count: bool = True) -> str:
        if not spans:
            return text
        # numaralama soldan sağa (deterministik), yerleştirme sağdan sola
        ordered = sorted(spans, key=lambda s: s.start)
        targets: dict[str, Span] = {}
        if self.cfg.name_detection.coreference:
            ordered, targets = link_person_mentions(ordered, session)
        fo, _sep, fc = self.cfg.fmt_parts
        parts: list[str] = []
        pos = 0
        for s in ordered:
            if s.start < pos:  # güvenlik: çakışan span (olmamalı) atlanır
                continue
            parts.append(text[pos : s.start])
            target = targets.get(s.normalized)
            if target is not None and target is not s and (s.entity, s.normalized) not in session.by_key:
                # kısa anılış tam addan önce geldi: yer tutucu tam adın değeriyle açılsın (geri çevirme tam adı yazar)
                session.placeholder_for(target, count=False)
            ph = session.placeholder_for(s, count)
            parts.append(ph)
            # "Ad <ayse@x.com>" → "<<EPOSTA_1>>": geri çevirmede dıştaki parantez metnin kendisidir, korunur
            if fo and fc and text.endswith(fo, 0, s.start) and text.startswith(fc, s.end):
                session.wrapped.add(ph)
            pos = s.end
        parts.append(text[pos:])
        return "".join(parts)

    def mask_text(self, text: str, session: MaskSession) -> tuple[str, list[Span], list[Span], list[Span]]:
        spans = self.analyze(text)
        m, b, lg = self.actionable(spans)
        return self.mask(text, m, session), m, b, lg
