"""Aynı kişinin kısa anılışları tam adın yer tutucusunu alır (name_detection.coreference)."""

from __future__ import annotations

import pytest
from conftest import engine_for

from matriks_pii.revert import Reverter
from matriks_pii.session import MaskSession

ON = {"name_detection": {"coreference": True}}


def _mask(text: str, override: dict | None = None, session: MaskSession | None = None) -> tuple[str, MaskSession]:
    eng = engine_for(ON if override is None else override)
    s = session or MaskSession(eng.cfg)
    masked, *_ = eng.mask_text(text, s)
    return masked, s


@pytest.mark.parametrize(
    "text,expect",
    [
        ("Ben Ayşe Demir. Ayşe'ye ulaşın.", "Ben <KISI_1>. <KISI_1>'ye ulaşın."),
        ("Ben Ayşe Demir. Demir Hanım da gelsin.", "Ben <KISI_1>. <KISI_1> Hanım da gelsin."),
        ("Ben Ayşe Demir. Sayın A. Demir aradı.", "Ben <KISI_1>. Sayın <KISI_1> aradı."),
        # kısa anılış önce: numara ilk anılışta kalır, sonraki kişi kaymaz
        ("Ayşe aradı, sonra Mehmet. Ayşe Demir'in dosyası hazır.", "<KISI_1> aradı, sonra <KISI_2>. <KISI_1>'in dosyası hazır."),
    ],
)
def test_short_mention_shares_full_name_placeholder(text: str, expect: str) -> None:
    assert _mask(text)[0] == expect


def test_ambiguous_first_name_keeps_separate_placeholder() -> None:
    masked, _ = _mask("Ayşe Demir ve Ayşe Kaya geldi. Ayşe çay istedi.")
    assert masked == "<KISI_1> ve <KISI_2> geldi. <KISI_3> çay istedi."


def test_initial_must_match_first_name() -> None:
    masked, _ = _mask("Ben Ayşe Demir. Sayın M. Demir aradı.")
    assert masked.startswith("Ben <KISI_1>.") and "<KISI_1> aradı" not in masked


def test_restore_value_is_full_name_even_when_short_mention_first() -> None:
    _, s = _mask("Ayşe aradı. Ayşe Demir'in dosyası hazır.")
    assert s.map == {"<KISI_1>": "Ayşe Demir"}
    rev = Reverter(dict(s.map), dict(s.entities), s.cfg)
    assert rev.revert("<KISI_1>'ye bilgi verildi.") == "Ayşe Demir'e bilgi verildi."


def test_links_across_messages_in_same_session() -> None:
    _, s = _mask("Müşteri Ayşe Demir başvurdu.")
    masked, _ = _mask("Ayşe Hanım'a dönüş yapın.", session=s)
    assert masked.startswith("<KISI_1>")


def test_entity_counts_not_inflated() -> None:
    _, s = _mask("Ayşe aradı. Ayşe Demir'in dosyası hazır.")
    assert s.entity_counts["KISI"] == 2


def test_default_off_keeps_previous_behaviour() -> None:
    masked, _ = _mask("Ben Ayşe Demir. Ayşe'ye ulaşın.", {})
    assert masked == "Ben <KISI_1>. <KISI_2>'ye ulaşın."


def test_hmac_ids_follow_full_name() -> None:
    cfg = {**ON, "placeholder_id": "hmac", "hash_secret": "0123456789abcdef0123456789abcdef"}
    masked, s = _mask("Ben Ayşe Demir. Ayşe'ye ulaşın.", cfg)
    assert len(s.map) == 1 and masked.count(next(iter(s.map))) == 2
