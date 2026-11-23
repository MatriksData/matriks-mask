# matriks-mask

[![PyPI](https://img.shields.io/pypi/v/matriks-mask.svg?cacheSeconds=3600)](https://pypi.org/project/matriks-mask/)
[![Python](https://img.shields.io/pypi/pyversions/matriks-mask.svg?cacheSeconds=3600)](https://pypi.org/project/matriks-mask/)
[![ci](https://github.com/MatriksData/matriks-mask/actions/workflows/ci.yml/badge.svg)](https://github.com/MatriksData/matriks-mask/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

[English](README.md) | **Türkçe**

KVKK uyum süreçlerini destekleyen, Türkçe ve **geri çevrilebilir** kişisel veri maskeleme eklentisi (LiteLLM guardrail).

> Bu eklenti KVKK uyum süreçlerini destekleyen teknik bir önlemdir; hukuki görüş değildir ve tek başına uyum sağlamaz
> (bkz. [KVKK ve sektör mevzuatı](#kvkk-ve-sektör-mevzuatı-hukuki-görüş-değildir)). **Eklentinin metindeki her kişisel
> veriyi tespit edeceği garanti edilmez** (bkz. [Bilinen sınırlar](#bilinen-sınırlar)); maskeleme sonuçları kullanım
> amacına göre kurum tarafından test edilmelidir.
>
> **Veri sorumluluğu:** Eklentiyi kendi sistemlerinde kullanan kurumlar, işledikleri kişisel veriler için veri
> sorumlusudur; Matriks bu verilerin veri sorumlusu değildir. Eklenti Matriks'e veri, telemetri veya kullanım bilgisi
> göndermez; tüm işlem kurumun kendi ortamında yapılır.

İstek LLM sağlayıcısına gitmeden önce TCKN, VKN, IBAN, kart, telefon, ad-soyad, adres, API anahtarı, KVKK m.6 özel
nitelikli veriler gibi değerleri `<KISI_1>`, `<TCKN_1>` biçimli yer tutucularla değiştirir; yanıt istemciye dönerken
(akışta da) yer tutucuları orijinal değerlere geri çevirir. Orijinal değerler sağlayıcıya, proxy loglarına, spend log'a,
metriklere ve denetim izine gitmez. Tüm işlem süreç içidir: tespit için veri yurt dışındaki bir hizmete gönderilmez.

```
Kullanıcı ──"Ben Ayşe Demir, TCKN 17291716060"──▶ LiteLLM proxy ──"Ben <KISI_1>, TCKN <TCKN_1>"──▶ LLM
Kullanıcı ◀──"Merhaba Ayşe Demir'e ilettim"────── matriks-mask ◀──"Merhaba <KISI_1>'e ilettim"───── LLM
                                                  (ek onarımı: 'e → 'e/'ye ünlü uyumuna göre)
```

## Öne çıkanlar

- **Türkçe tanımlayıcılar:** TCKN, VKN, TR IBAN, Troy, TR plaka, MERSİS, MKK sicil; kontrol basamağı ve bağlam doğrulamalı.
- **Türkçe adlar ve ekli biçimler** (`Ahmet'in`, `Ayşeye`, `Ahmet YILMAZ`): ad sözlüğü + morfoloji + bağlam
  (+ opsiyonel Türkçe NER).
- **Yanıtta geri çevirme (akış dâhil):** ünlü uyumuna göre ek onarımı (`<KISI_1>'e` → `Kemal'e`/`Ayşe'ye`).
- **KVKK m.6 özel nitelikli veri:** 12 kategori, kişiye bağlılık şartıyla.
- **Süreç içi çalışma:** tespit için metin harici bir hizmete gönderilmez.
- **Düşük gecikme:** 2 KB metinde p50 ≈ 2.9 ms (geliştirme ortamında ölçülmüştür; donanıma göre değişir,
  ölçüm: `pytest -m benchmark`, `python scripts/bench.py`).

## Kurulum

Paket ve komut adı [**`matriks-mask`**](https://pypi.org/project/matriks-mask/). Python modülü (`matriks_pii`,
LiteLLM guardrail yolu `matriks_pii.guardrail.TurkishPIIGuardrail`), `MATRIKS_PII_*` ortam değişkenleri ve
`matriks_pii_*` metrikleri adını korur; mevcut yapılandırmalar değişmeden çalışır. Eski komut adı `matriks-pii` de
kurulur. Git'ten kurulmuş `matriks-pii` ≤ 0.3.3'ten geçerken önce `pip uninstall matriks-pii` çalıştırın.

```bash
pip install "matriks-mask[fast]"                 # fast: pyahocorasick (opsiyonel, daha hızlı sözlük araması)
pip install "matriks-mask[redis,fast,metrics]"   # üretim: şifreli Redis oturum deposu + Prometheus
pip install "matriks-mask[ner]"                  # opsiyonel Türkçe NER (transformers + torch)
pip install "git+https://github.com/MatriksData/matriks-mask"   # main dalındaki en son sürüm
pipx install matriks-mask                        # yalnız komut (`matriks-mask "…"`), yalıtılmış ortam
```

Üretimde sabit sürümler için depodaki [`constraints/prod.txt`](constraints/prod.txt) ile kurun
(`pip install "matriks-mask[redis,fast,metrics]" -c constraints/prod.txt`).

### LiteLLM olmadan deneme

```bash
$ matriks-mask "Ben Ayşe Demir, TCKN 17291716060"
Ben <KISI_1>, TCKN <TCKN_1>
$ echo "Ahmet Yılmaz'ın telefonu 0532 234 56 78" | matriks-mask mask --profile strict --json   # tür + konum, değer yok
```

Python'dan (maskele → geri çevir):

```python
from matriks_pii import MaskSession, PIIEngine, Reverter, TurkishPIIConfig

cfg = TurkishPIIConfig.build({"profile": "balanced"})
engine, session = PIIEngine(cfg), MaskSession(cfg)
masked, spans, blocked, logged = engine.mask_text("Ben Ayşe Demir, TCKN 17291716060", session)
print(masked)                                    # Ben <KISI_1>, TCKN <TCKN_1>
reverter = Reverter(session.map, session.entities, cfg)
print(reverter.revert("<KISI_1>'e ilettim", final=True))   # Ayşe Demir'e ilettim
```

`session.map` (yer tutucu → orijinal değer) kişisel veri içerir; loglamayın, kalıcı olarak saklamayın. `blocked` boş
değilse (BLOCK aksiyonlu varlık) metni göndermeyin.

### LiteLLM ile

Asgari LiteLLM sürümü **1.106.1** (1.106.1, 1.106.5 ve 1.110.2 ile test edildi). LiteLLM `config.yaml`:

```yaml
guardrails:
  - guardrail_name: matriks-pii
    litellm_params:
      guardrail: matriks_pii.guardrail.TurkishPIIGuardrail
      mode: [pre_call, post_call]   # ikisi de ZORUNLU (post_call = geri çevirme)
      default_on: true
      profile: balanced
```

Kurulumu doğrulayın (değersiz; CI'da ve k8s `startupProbe` olarak da kullanılabilir):

```bash
python -m matriks_pii.selftest --config config.yaml           # config, depo, 12 kanonik sondaj, gecikme
python -m matriks_pii.selftest --config config.yaml --quick   # < 2 sn
```

Adım adım kurulum, çoklu worker/replika, doğrulama ve sorun giderme: [`docs/LITELLM_KURULUM.md`](docs/LITELLM_KURULUM.md).
İşletim el kitabı (hata kodları, metrikler, sır rotasyonu, geri alma): [`docs/RUNBOOK.md`](docs/RUNBOOK.md).
KVKK veri kategorileri eşlemesi (VERBİS/DPIA): [`docs/KVKK_VERI_KATEGORILERI.md`](docs/KVKK_VERI_KATEGORILERI.md).

Tam örnekler: [`examples/config.yaml`](examples/config.yaml), [`config.strict.yaml`](examples/config.strict.yaml),
[`config.redis.yaml`](examples/config.redis.yaml), [`config.placeholder_formats.yaml`](examples/config.placeholder_formats.yaml),
[`Dockerfile`](examples/Dockerfile).

`default_on: false` ise istek gövdesinde `"guardrails": ["matriks-pii"]` ile açılır. İstemci guardrail'i kapatamaz:
`disable_global_guardrails` / `opted_out_global_guardrails` yalnız yönetici (anahtar/ekip) metadata'sından okunur.

**`stream_scope` uyarısı:** LiteLLM guardrail ayarındaki `stream_scope: non_streaming` akış yanıtlarında `post_call`
kancasını atlar; yer tutucular istemciye geri çevrilmeden gider. Bu ayarı kullanmayın (selftest uyarır).

## Tespit edilen varlıklar

| Etiket | Ne | Nasıl doğrulanır | Varsayılan |
|---|---|---|---|
| `TCKN` | T.C. kimlik no (98/99 ile başlayan yabancı kimlik no dâhil) | 11 hane + resmi checksum; `17.291.716.060`, tek tek yazım, tam genişlik rakam | MASK |
| `VKN` | Vergi kimlik no | 10 hane + checksum **ve** vergi bağlamı | MASK |
| `IBAN` | TR ve yabancı IBAN (boşluklu/tireli/küçük harf; etiketliyse `TR`'siz) | ISO 7064 mod-97 | MASK |
| `KART` | Visa/MC/Amex/Discover/JCB/Diners/**Troy** | Luhn + BIN | MASK, yanıtta yalnız son 4 hane |
| `KART_CVV`, `KART_SKT` | CVV, son kullanma | bağlam | MASK (CVV asla geri yüklenmez) |
| `TELEFON` | mobil, sabit hat (gerçek alan kodları), 0850, 0812 | numaralandırma planı; kayıt numarası bağlamı dışlanır | MASK (444/0800 opsiyonel) |
| `EPOSTA`, `IP`, `MAC` | `[at]`/`(at)` ile gizlenmiş e-posta dâhil | IPv4/IPv6 doğrulaması, sürüm numarası dışlaması | MASK (MAC: IGNORE) |
| `PLAKA` | `34 ABC 123`, `06-A-1234` | il kodu 01–81, harf/rakam uzunluk kuralı | MASK |
| `PASAPORT`, `KIMLIK_SERI`, `EHLIYET`, `SGK`, `NUFUS_KAYIT` | kimlik belgeleri (eski nüfus cüzdanı dâhil) | bağlam zorunlu | MASK |
| `HESAP_NO`, `MUSTERI_NO`, `MKK_SICIL`, `POLICE_NO`, `MERSIS`, `ETTN`, `IMEI`, `SASI_NO`, `ADRES_KODU` | finans/kurumsal kimlikler | bağlam (+Luhn/VKN) | MASK |
| `TICARET_SICIL` | ticaret sicil no | bağlam | IGNORE (tüzel kişi; şahıs işletmeleri için açılabilir) |
| `KRIPTO_CUZDAN` | BTC/ETH/TRON adresi | biçim | IGNORE |
| `SIFRE` | `şifre: …`, `password=…` (değer rakam/sembol içermeli) | bağlam | MASK, geri yükleme yok (yanıtta `****`); strict: **BLOCK** |
| `GIZLI_ANAHTAR` | OpenAI/Anthropic/AWS/GitHub/Slack/Google/Stripe anahtarı, JWT, özel anahtar, bağlantı dizesi parolası, Bearer | sağlayıcı önekleri | MASK, yanıtta `****` (strict: BLOCK) |
| `KONUM` | kişiye/kişisel kayda bağlı koordinat | bağlam + kişiye bağlılık | MASK |
| `DOGUM_TARIHI`, `DOGUM_YERI` | | doğum bağlamı | MASK |
| `ADRES` | mahalle/sokak/cadde/no/daire, köy/mevkii… | ≥2 çapa veya `No`/numara + il/ilçe | MASK |
| `KISI` | ad, ad-soyad, `Ahmet YILMAZ`, unvanlı ad, `Ahmetin`, `AHMET'in` | 7k ad + 23.9k soyad sözlüğü, bağlam, morfoloji, kurum adı dışlaması | MASK |
| `OZEL_SAGLIK`, `OZEL_GENETIK`, `OZEL_BIYOMETRIK`, `OZEL_CINSEL`, `OZEL_CEZA` | KVKK m.6 | terim + **kişiye bağlılık** (aynı cümlede kişi/kimlik veya 1./2. şahıs) | MASK |
| `OZEL_DIN`, `OZEL_ETNIK`, `OZEL_SIYASI`, `OZEL_FELSEFI`, `OZEL_KIYAFET`, `OZEL_UYELIK`, `OZEL_CEZA_GENIS` | KVKK m.6 | aynı | LOG (yalnız sayılır) |

Aksiyonlar: `MASK` (yer tutucu, yanıtta geri çevrilir), `BLOCK` (HTTP 400, değer içermeyen hata), `LOG` (yalnız denetim izinde sayım), `IGNORE`.

## Kuruma özel tanımlar

Yerleşik kurallar kurumdan bağımsız biçimleri kapsar (TCKN, IBAN, telefon, e-posta, URL kodlaması…). Kurumdan kuruma
değişen biçimler (log alanındaki hesap no, PNR, kullanıcı adı, sipariş no) **yapılandırmayla** eklenir; koda dokunmak
gerekmez. `entity: KULLANICI_ADI` yazınca `<KULLANICI_ADI_1>` yer tutucusu oluşur ve yanıtta geri çevrilir.

| Alan | Ne zaman | Örnek |
|---|---|---|
| `extra_regex` | Biçimi belli, kuruma özel kimlikler | hesap no, PNR, kullanıcı adı |
| `deny_list` | Her zaman maskelenecek sabit ifadeler | proje adı, müşteri kodu |
| `allow_list` | Asla maskelenmeyecek ifadeler | ürün adı gibi görünen kişi adları (TCKN, VKN, IBAN, kart, CVV, anahtar ve şifrede uygulanmaz) |

`extra_regex` alanları:

| Alan | Varsayılan | Açıklama |
|---|---|---|
| `entity` | zorunlu | Yeni bir ad (ASCII büyük harf, `_`) ya da mevcut bir etiket (ör. `TELEFON`; o zaman o varlığın aksiyonu geçerlidir) |
| `pattern` | zorunlu | Python regex |
| `group` | — | Yalnız bu yakalama grubu maskelenir (numara veya ad): `'AccountId=([0-9]+)'` + `group: 1` → yalnız sayı |
| `score` | 0.8 | Çakışmada önceliği belirler |
| `context` | — | Regex; değerin `context_window` karakter yakınında bu ifade varsa eşleşir. Büyük/küçük harf ve Türkçe harf duyarsızdır (`reçete` = `RECETE`) |
| `context_window` | 60 | Bağlamın arandığı uzaklık; geniş kalırsa komşu sayılar da yakalanır, daraltın |
| `ignore_case` | `true` | `pattern` için; büyük harfli kodlarda (PNR) `false` yapın |
| `action` | `MASK` | `MASK`, `BLOCK`, `LOG`, `IGNORE` (yalnız yeni türlerde) |
| `restore` | `true` | `false`: yanıtta geri çevrilmez (`****`) |

```yaml
extra_regex:
  - {entity: HESAP_NO, pattern: '(?:AccountId=|\|1=)([0-9]{6,12})', group: 1, score: 0.9}
  - {entity: PNR, pattern: '(?<![0-9A-Za-z])[A-Z0-9]{6}(?![0-9A-Za-z])', context: 'pnr|rezervasyon kodu', context_window: 25, ignore_case: false}
deny_list:
  - {phrase: "Proje Kartal", entity: PROJE}        # → <PROJE_1>, yanıtta geri
allow_list: ["Can Bey"]
```

**Hazır sektör örnekleri** ([`examples/sektor/`](examples/sektor)): finans/aracı kurum, havayolu, sağlık, sigorta, sanayi,
e-ticaret/kargo, telekom ve yurt dışı biçimler (uluslararası telefon, ABD SSN, UK NINO). Her dosya kendi ölçüm
kümesiyle ([`tests/fixtures/sektor/`](tests/fixtures/sektor)) CI'da sınanır. Başlangıç noktasıdırlar; kendi gerçek
biçimlerinize göre daraltın. Yeni sektör dosyası katkısı için: [`CONTRIBUTING.md`](CONTRIBUTING.md).

**Yerleşik sektör türleri:** `HESAP_NO`, `MUSTERI_NO`, `MKK_SICIL`, `POLICE_NO` finansa/sigortaya özgüdür ama
yalnız kendi bağlamlarında ("hesap no", "MKK sicil", "poliçe no") tetiklenir; başka sektörde yanlış pozitif üretmez.

### Kural yazma ve doğrulama

```bash
# 1) kuralı config'e yaz, tek metinle dene (çıktıda değer yok; --show-values yalnız yerel hata ayıklama için)
matriks-mask selftest --config config.yaml --probe "PNR: TX4K9Q yolcu Ayşe Demir"
#   probe: entity=PNR start=5 end=11 score=0.8 recognizer=extra_regex
matriks-mask mask --config config.yaml "PNR: TX4K9Q yolcu Ayşe Demir"
#   PNR: <PNR_1> yolcu <KISI_1>

# 2) kendi etiketli kümenizle ölçün (satır başına bir JSON; maskelenmemesi gereken satırlar "entities": [])
matriks-mask selftest --config config.yaml --corpus kume.jsonl
```

```json
{"text": "PNR: TX4K9Q yolcu Ayşe Demir", "entities": [{"entity": "PNR", "value": "TX4K9Q"}, {"entity": "KISI", "value": "Ayşe Demir"}]}
{"text": "TK1984 seferi 14:30 kalkış, kapı B12", "entities": []}
```

> **Ürünü değerlendirirken:** Ölçümden önce kurumunuza özel biçimleri tanımlayın ve kapsam dışı türleri (ör. kullanıcı
> adı tanımlamadıysanız) paydaya katmayın; aksi hâlde sonuç ürünü değil yapılandırma eksiğini ölçer. Kuralları yazdığınız
> örneklerle değil, kuralların görmediği ayrı bir etiketli kümeyle ölçün.

### Yurt içi / yurt dışı kapsam

Çekirdek Türkiye'ye göre yazılmıştır:

| Veri | Türkiye | Yurt dışı |
|---|---|---|
| Ulusal kimlik no | TCKN, VKN | yerleşik değil (SSN, NINO: [`yurtdisi.yaml`](examples/sektor/yurtdisi.yaml)) |
| Telefon | yakalanır | yerleşik değil ([`yurtdisi.yaml`](examples/sektor/yurtdisi.yaml)) |
| IBAN, kart | yakalanır | yakalanır |
| Pasaport | bağlamla | bağlamla |
| E-posta, IP | yakalanır | yakalanır |
| Ad soyad | sözlük + morfoloji | yalnız NER modunda (`ner_backend: hf`) |
| Adres | yakalanır | yakalanmaz |

## Profiller

| Profil | Ne yapar |
|---|---|
| `balanced` (varsayılan) | Yukarıdaki tablo |
| `strict` | Tüm `OZEL_*`, kripto, MAC, ticaret sicil MASK; `GIZLI_ANAHTAR` BLOCK; 444 hatları MASK; LLM'e yer tutucu talimatı; yanıtta yeni PII taraması; etiketli hatalı checksum'lı değerler de maskelenir |
| `minimal` | Yalnız TCKN, VKN, IBAN, KART, TELEFON, EPOSTA, KISI, GIZLI_ANAHTAR; `OZEL_*` LOG |

Açık anahtarlar profili ezer: `entities: {OZEL_DIN: MASK, PLAKA: IGNORE}`. Bilinmeyen varlık adı başlangıçta hata verir.

## Önemli ayarlar

| Ayar | Varsayılan | Açıklama |
|---|---|---|
| `placeholder_format` | `<{label}_{n}>` | `__{label}_{n}__`, `[{label}_{n}]`, `PII_{label}_{n}` … |
| `placeholder_id` | `sequence` | `hmac` + `hash_secret`: aynı değer turlar/replikalar arası aynı yer tutucu (prompt cache dostu; kayan pencerede numara değişmez) |
| `fail_mode` | `closed` | Guardrail hata verirse istek LLM'e **gitmez** (HTTP 500) |
| `revert_fail_mode` | `closed` | Geri çevirme hatasında **veya oturum bulunamazsa** yanıt istemciye yer tutucuyla gitmez (HTTP 500) |
| `dry_run` | `false` | Gölge mod: hiçbir şey maskelenmez/engellenmez, yalnız "maskelenecekti" sayımları loglanır (kademeli yayın) |
| `mask_logs` | `true` | Proxy'nin logladığı (geri çevrilmiş) yanıtı ve isteği yeniden maskeler |
| `session_store` | `memory`, TTL 3600 s, 10.000 oturum | `redis`: AES-GCM şifreli; `redis_timeout_seconds` (2 s), devre kesici, `require_tls`, `encryption_key_previous` (rotasyon) |
| `session_secret` | — (`MATRIKS_PII_SESSION_SECRET`) | Oturum jetonu HMAC sırrı, ≥32 karakter; `redis` deposunda **zorunlu**, tüm örneklerde aynı; `session_secret_previous` (rotasyon) |
| `max_text_chars`, `max_request_chars` | 2 MB, 4 MB | Aşılırsa istek temizlenir, HTTP 413 |
| `analysis_threads` | 4 | Analiz/log maskeleme için ayrılmış iş parçacığı havuzu (LiteLLM'in executor'ını aç bırakmaz) |
| `analysis_cache` | 2048 girdi, 300 s, 200k span | Bellekte span listesi (orijinal değerlerle) tutar; `max_entries: 0` kapatır; 64 KB üstü metin önbelleğe girmez |
| `unsupported_call_types` | `mask` | rerank/görsel/ses/MCP çağrılarında: `mask` (geri çevirmesiz), `pass`, `block` |
| `gemini_native` | `true` | Gemini `/v1beta/models/...:generateContent` (+ stream) tam maskele/geri çevir |
| `audit_log` | kapalı | Değersiz JSONL denetim izi (kim, ne zaman, hangi varlık türünden kaç adet, skor kovaları) |
| `metrics_enabled` | `true` | `prometheus_client` kuruluysa `matriks_pii_*` sayaçları (değersiz etiketler) |
| `placeholder_instruction` | `none` | `system_suffix` (önerilir): system mesajının sonuna talimat eklenir: yer tutucuyu gerçek değer gibi kullan, maskelemeden söz etme, kısaltma |
| `label_aliases` | `PERSON→KISI, PHONE→TELEFON…` | LLM'in çevirdiği etiketler (`<PERSON_1>`) yalnız parantezli biçimde geri çevrilir; `{}` kapatır |
| `scan_response` | `false` (`strict`: açık) | Yanıtta LLM'in ürettiği YENİ kişisel veriyi maskeler (geri çevrilmez; akışta uygulanmaz) |
| `response_json_preserve_format` | `true` | Yanıt içeriğindeki JSON metin biçimi (girinti, sıra) korunarak geri çevrilir |
| `suffix_repair` / `suffix_repair_attached` | `true` / `false` | Türkçe ek onarımı; apostrofsuz bitişik ek (`<KISI_1>lar` → `Demirler`) bayrakla |
| `tool_restore_policy` | `all` | `none`: tool çağrı argümanları yer tutucuyla kalır |
| `mask_system_messages`, `mask_assistant_messages`, `mask_tool_messages`, `mask_embeddings` | `true` | |
| `name_detection.ner_backend` | `none` | `hf`: Türkçe NER; `ner_max_chars` (cümle bazlı parçalama), `ner_timeout_seconds`, `ner_fail_mode` (`closed`/`lexicon`), `ner_preload` |
| `name_detection.coreference` | `false` | `true`: aynı kişinin kısa anılışı (`Ayşe Demir` → `Ayşe'ye`, `Demir Hanım`, `A. Demir`) tek bir tam adla eşleşirse aynı yer tutucuyu alır (model iki kişi sanmaz); belirsizlikte (iki `Ayşe`) ayrı kalır. Yanıtta kısa anılış tam adla geri çevrilir |
| `checksum_policy` | `strict` (`strict` profil: `context`) | `context`: açık etiketli değer kontrol basamağı tutmasa da maskelenir |

Ortam değişkenleri `os.environ/AD` biçimiyle verilebilir (`hash_secret: os.environ/MATRIKS_PII_HASH_SECRET`). Sırlar
düz metin verilirse başlangıçta uyarı yazılır (`MATRIKS_PII_STRICT_SECRETS=1` → hata). Sırlar `config_hash`'e, hata
iletilerine ve loglara girmez.

### Hata durumları

| Durum | HTTP | `detail.error` | Ne olur |
|---|---|---|---|
| Politika gereği engelleme (`BLOCK`) | 400 | `matriks_pii_blocked` | `entity` alanı var, değer yok; istek loglanmadan önce maskelenir |
| Metin/istek çok büyük | 413 | `matriks_pii_text_too_large` | istek temizlenir; `fail_mode`'dan bağımsız |
| Guardrail iç hatası | 500 | `matriks_pii_guardrail_failure` | `fail_mode: closed` → istek temizlenir, sağlayıcıya gitmez |
| Oturum bulunamadı (TTL, sır uyuşmazlığı, depo) | 500 | `matriks_pii_session_missing` | `revert_fail_mode: closed` → yanıt yer tutucuyla gitmez |
| Oturum deposuna erişilemiyor | 503 | `matriks_pii_store_unavailable` | yalnız PII içeren istekler etkilenir (PII'siz isteklerde oturum yazılmaz) |
| Geri çevirme hatası | 500 | `matriks_pii_revert_failure` | `revert_fail_mode: open` → yanıt yer tutuculu döner |

Akışta (stream) hata ilk parçadan önce oluşursa LiteLLM bunu SSE hata çerçevesine çevirir (HTTP durumu 200 olarak
gitmiş olabilir); hiçbir durumda ham değer veya yer tutucu yayılmaz.

### Çoklu worker / çoklu replika kurulumu

Bir isteğin pre-call ve post-call kancaları aynı süreçte çalışır; tek pod'da `memory` deposu yeterlidir. Redis şu
durumlarda önerilir: çok pod/replika arkasında Admin UI "guardrail test" (`apply_guardrail`) çiftleri, oturumların
yeniden başlatmalarda korunması ve bellek sınırının süreç dışına taşınması.

1. Sırları **bir kez** üretin ve secret manager'a koyun: `python -m matriks_pii.keys` (`--k8s` ile kubectl komutu).
2. `pip install "matriks-mask[redis,fast]"`.
3. [`examples/config.redis.yaml`](examples/config.redis.yaml):

   ```yaml
   guardrails:
     - guardrail_name: matriks-pii
       litellm_params:
         guardrail: matriks_pii.guardrail.TurkishPIIGuardrail
         mode: [pre_call, post_call]
         default_on: true
         session_secret: os.environ/MATRIKS_PII_SESSION_SECRET
         session_store:
           backend: redis
           redis_url: os.environ/MATRIKS_PII_REDIS_URL      # rediss://:parola@redis:6380/0 (TLS önerilir)
           encryption_key: os.environ/MATRIKS_PII_STORE_KEY
           ttl_seconds: 3600                                  # ≥ LiteLLM request_timeout
           redis_timeout_seconds: 2
           require_tls: true
   ```

Kesintisiz sır/anahtar rotasyonu için `*_PREVIOUS` değişkenleri: [`docs/RUNBOOK.md`](docs/RUNBOOK.md#sır-ve-anahtar-rotasyonu-kesintisiz).

### Kiracı (anahtar/ekip) bazlı politika

Farklı politika için birden çok guardrail örneği tanımlayın (`matriks-pii`, `matriks-pii-strict` …, `default_on: false`)
ve anahtar/ekip `guardrails` listesine ekleyin. Oturum jetonları guardrail adına göre ayrıdır; iki örnek aynı istekte
birlikte çalışabilir.

## Desteklenen çağrılar

| Uç nokta | İstek | Yanıt | Akış |
|---|---|---|---|
| `/chat/completions` | metin, çok parçalı içerik, `tool_calls` JSON argümanları, `name`, `reasoning_content` | içerik, tool çağrıları, `reasoning_content`, `n>1` | içerik, reasoning, tool argümanları (JSON kaçışlı) |
| `/completions`, `/embeddings` | `prompt`, `input` (str/list) | `choices[].text` | `/completions`: `choices[].text` parçaları |
| `/responses` | `input`, `instructions`, `function_call(_output)`, `prompt.variables` | `output` | `response.*.delta/done/completed` olayları |
| `/v1/messages` (Anthropic) | `system`, `tool_use`, `tool_result`, `document` | `content` | ham SSE (`text_delta`, `input_json_delta`), bayt düzeyinde UTF-8 güvenli |
| `/v1beta/models/...:generateContent` (Gemini) | `contents[].parts[]`, `systemInstruction`, `functionCall/Response` | `candidates[].content.parts[]` | ham SSE çerçeveleri, aday başına tampon |
| rerank, görsel, ses, MCP | metin alanları maskelenir (`unsupported_call_types`) | geri çevrilmez | — |

Biçimler arası eşdeğerlik `tests/test_parity.py` matrisiyle (5 biçim × 15 özellik) doğrulanır; uygulanamayan hücreler
orada gerekçesiyle listelenir.

JSON içerikli metinler (tool argümanı, JSON mesaj, sayı ve anahtarlar dâhil) çözülüp yaprak bazında maskelenir; yanıttaki
JSON içerik biçimi korunarak geri çevrilir.

## Kalite ve performans

Tüm testler: `pytest -q` (≈2.3k test + hypothesis özellik testleri), `pytest -m benchmark`, `pytest -m e2e` (gerçek
LiteLLM proxy + OpenAI uyumlu ve Gemini echo sunucusu), `pytest -m external`, `pytest -m ner`, `pytest -m redis_live`.

- Pozitif/karışık fixture'lar (733 tespit + 76 geri çevirme + 37 akış + 94 kanca senaryosu + 52 sektör örneği satırı) ve **596 yanlış pozitif
  tuzağı** (hisse kodları, ISIN, VIOP, geçersiz checksum'lı sayılar, kurum adları, kur çiftleri, gizli anahtar önekleri,
  özel kategori haber cümleleri…): `python scripts/corpus_report.py`. Varsayılan sözlük modunda yanlış pozitif sayısı 0'dır.
- Uygulama kodunu görmeyen ayrı LLM ajanlarının (yapay zekâ modelleri) yazdığı iki sentetik değerlendirme kümesi: `tests/fixtures/holdout/` (sonuçlar aşağıda).

| Küme | Satır (PII / tuzak) | Ölçüm | P | R | F2 |
|---|---|---|---|---|---|
| holdout-1 (`holdout.jsonl`), KISI | 269 (162 / 107) | kör, düzeltme öncesi | 0.947 | 0.831 | 0.852 |
| holdout-1, KISI | | genel düzeltmeler sonrası | 0.977 | 0.977 | 0.977 |
| holdout-2 (`holdout2.jsonl`), tüm varlıklar, `balanced` | 345 (226 / 119) | kör, düzeltme öncesi | 0.954 | 0.934 | 0.938 |
| holdout-2, tüm varlıklar, `balanced` | | genel düzeltmeler sonrası | 0.991 | 0.994 | 0.993 |
| holdout-2, tüm varlıklar, `strict` | | genel düzeltmeler sonrası | 0.974 | 0.994 | 0.990 |

Holdout-2 yeni varlıkları (`GIZLI_ANAHTAR`, `KONUM`, `MKK_SICIL`, `POLICE_NO`) ve 0.3.0 kurallarını da kapsar. Kör ölçümden
sonra yalnız kümeye özel olmayan düzeltmeler yapıldı; kalan 3 satır yorum farkıdır ("Velinin" belirsizliği, "Ramazan Usta"
tam ad mı, "kan grubu 0 Rh-" span sınırı). Kümeler artık kör olmadığından `tests/test_holdout.py` yalnız gerilemeyi yakalar
(deterministik varlıklarda FP = FN = 0; KISI R ≥ 0.95, F2 ≥ 0.95; holdout-2'de OZEL_* recall ≥ 0.90).

NER modu (`ner_backend: hf`, `akdeniz27/bert-base-turkish-cased-ner`; `python scripts/corpus_report.py --ner`): fixture'larda
`KISI` F2 ≈ 0.99; 512 token sınırı cümle bazlı parçalamayla aşılır. NER modunda negatif set bilgi amaçlıdır (model gerçek
yabancı adları da yakalar).

**Üçüncü taraf Türkçe veri setleri** (`python scripts/external_bench.py --download`; geliştirmede hiçbir satır fixture'a
kopyalanmadı, yalnız genel kurallar düzeltildi). Kapsam dışı etiketler (uyruk, maaş, kullanıcı adı, kredi notu…) hesaba katılmaz:

| Veri seti | Profil | P | R | F2 | Not |
|---|---|---|---|---|---|
| [cagrigungor/turkish-pii-masking-benchmark](https://huggingface.co/datasets/cagrigungor/turkish-pii-masking-benchmark) (1.000 satır, CC0) | `strict` | 0.978 | 0.944 | **0.950** | poliçe no ve koordinat artık kapsamda (payda büyüdü); eski kapsamda FP 58 → 51 |
| aynı | `balanced` | 0.985 | 0.749 | 0.787 | farkın çoğu LOG'lanan özel kategoriler (din, etnik köken, sendika) |
| [Werea-co/Werea-KVKK-Bench](https://huggingface.co/datasets/Werea-co/Werea-KVKK-Bench) `pii_synthetic` (2.400 satır, Apache-2.0) | `strict` | **1.000** | 0.627 | 0.677 | kaçırılanların çoğu kişiye bağlanmamış genel özel kategori cümleleri — KVKK m.3/1-d gereği bilinçli |

Gecikme (tek çekirdek, NER kapalı, önbellek kapalı; `python scripts/bench.py`):

| Girdi | p50 (ms) | p99 (ms) |
|---|---|---|
| 256 karakter | 0.51 | 0.96 |
| 2048 karakter | 2.8–3.0 | 3.3–3.5 |
| 10000 karakter | 14.0 | 21.9 |
| 50000 karakter | 79.9 | 93.6 |
| akış parçası (5 karakter) | 0.012 | — |

8 KB'den büyük toplam metin (NER açıkken her metin) ayrılmış iş parçacığı havuzunda analiz edilir; olay döngüsü
bloklanmaz. Patolojik girdiler (100 KB rakam dizisi, binlerce geçersiz IBAN adayı, uzun boşluk dizileri) doğrusal sürede işlenir.

## İşletim

- **Metrikler** (`pip install "matriks-mask[metrics]"`, LiteLLM'de `callbacks: ["prometheus"]`): `matriks_pii_entities_total`,
  `matriks_pii_hook_seconds`, `matriks_pii_hook_total`, `matriks_pii_events_total` (session_missing, store_unavailable,
  blocked, unresolved_placeholder, ner_failed, audit_write_failed, dry_run_would_mask), `matriks_pii_scores_total`,
  `matriks_pii_sessions_open`, `matriks_pii_cache_entries`. Çoklu worker modunda güvenlidir.
- **Kademeli yayın:** önce `dry_run: true` (staging 5 iş günü, üretim 1 hafta), sonra anahtar bazlı canary, sonra `default_on`.
- **Geri alma:** wheel + config çift olarak; acil durumda `MATRIKS_PII_IGNORE_UNKNOWN_KEYS=1`. Ayrıntı: [`docs/RUNBOOK.md`](docs/RUNBOOK.md).

## KVKK ve sektör mevzuatı (hukuki görüş değildir)

- **Terim:** Harita bellekte tutulduğu sürece bu işlem anonimleştirme değil, "kod/takma isim" kullanımıdır
  (Kişisel Verilerin Silinmesi, Yok Edilmesi veya Anonim Hale Getirilmesi Rehberi; Üretken Yapay Zekâ ve Kişisel Verilerin
  Korunması Rehberi (15 Soruda), 24.11.2025). Veri sorumlusu nezdinde kişisel veri
  olmaya devam eder; m.28/1-b istisnasına dayanılamaz.
- **Yurt dışı aktarım (m.9, Yönetmelik 10.07.2024):** Yurt dışındaki LLM'e istek aktarımdır; maskeleme yükümlülüğü kaldırmaz,
  riski azaltır. Standart Sözleşme-2 imzalanıp 5 iş günü içinde Kurum'a bildirilmelidir. Sürekli API trafiği "arızi" değildir.
- **Özel nitelikli veri (m.6):** Sağlık/genetik/biyometrik/cinsel/ceza verisi varsayılan MASK; `profile: strict` hepsini maskeler,
  `BLOCK` ile hiç gönderilmemesi sağlanır. Ceza mahkûmiyeti kesinleşmiş hükümdür; şüpheli/sanık/gözaltı ayrı (`OZEL_CEZA_GENIS`).
- **Veri güvenliği (m.12):** fail-closed varsayılan, değersiz denetim izi ve metrikler, loglarda maskeleme, Redis'te AES-GCM,
  harita asla metadata'ya yazılmaz (`store_map_in_metadata` yalnız `MATRIKS_PII_ALLOW_DEBUG_MAP=1` ile), API anahtarları maskelenir.
- **Sermaye piyasası ve bankacılık mevzuatı:** Bu sektörlerdeki kurumlar ayrıca SPK'nın Bilgi Sistemleri Yönetimine
  İlişkin Usul ve Esaslar Tebliği (VII-128.10), 5411 sayılı Bankacılık Kanunu m.73 (sır saklama) ve BDDK'nın Sır
  Niteliğindeki Bilgilerin Paylaşılması Hakkında Yönetmeliği gibi düzenlemelere tabidir; eklenti bu yükümlülükleri
  karşılamaz. Bankalar, aracın Sır Yönetmeliği kapsamında kimliksizleştirme amacıyla kullanılmaya uygun olup olmadığını
  kendileri değerlendirmelidir.
- **Test verileri:** Depodaki test fixture'larının tamamı sentetiktir; gerçek kişilere ait veri içermez.

Kategori eşlemesi ve denetim izi alanları: [`docs/KVKK_VERI_KATEGORILERI.md`](docs/KVKK_VERI_KATEGORILERI.md).

**Eklenti dışı yükümlülükler:** SCC-2 + bildirim, sağlayıcı veri işleme sözleşmesi (alt işleyenler, eğitimde kullanmama,
saklama süresi), DPIA, aydınlatma metni, VERBİS/envanter, ihlal müdahale planı (72 saat), çalışan eğitimi, kırmızı takım testleri.

## Bilinen sınırlar

- Tümü küçük harfle yazılmış serbest metin adları (`ahmet yılmaz dün geldi`) yalnız form/alan bağlamında yakalanır; NER modu
  önerilir. Kurum unvanı içindeki adlar (`Ahmet Yılmaz Holding`) maskelenmez; şahıs işletmesi adları (`Mehmet Kaya Kuyumculuk`)
  kişisel veri sayılıp maskelenir.
- Yabancı adlar (sözlükte yok) sözlük modunda ancak bağlamla yakalanır; NER modu önerilir. Sözcük olan tek adlar cümle
  ortasında (`Dün Deniz aradı`) yakalanır, cümle başında bağlamsız (`Deniz kenarında`) yakalanmaz.
- Sıfır genişlikli karakterle bölünmüş sayılar (`1729​1716060`) tespit edilmez. Tam genişlik rakamlar (`１７２９…`) tespit edilir.
- URL kodlu (`%40`, `%20`; yalnız URL/form içinde, Türkçe yüzde yazımı "%40" hariç) ve HTML varlıklı (`&#64;`) değerler
  ile URL/form parçasında harf/rakam arasındaki `+` (`tel=0532+123+45+67`) çözülerek taranır, orijinal kodlu aralık
  maskelenir. Çözülmeyenler: geçersiz bayt içeren dizi (`%40%FF`), üç kat kodlama (`%252540`), URL/form dışındaki tek
  `%XX` dizisi ve Base64 gibi diğer kodlamalar.
- Yabancı telefon numaraları ve yabancı ulusal kimlik numaraları yerleşik değildir; bkz. [`yurtdisi.yaml`](examples/sektor/yurtdisi.yaml).
- Görsel/dosya (PDF, kimlik kartı fotoğrafı) içindeki kişisel veri taranmaz; bu içerik sağlayıcıya olduğu gibi gider.
- Responses API `previous_response_id` ile sunucu tarafında tutulan geçmiş maskelenemez.
- Anthropic `/anthropic/*` **passthrough** akışında LiteLLM post-call guardrail'i çağırmaz (LiteLLM #42476); `/v1/messages` kullanın.
- Anthropic imzalı `thinking` blokları ve Gemini imzalı düşünce parçaları değiştirilmez (imza bozulur).
- Çok turlu konuşmada `sequence` numaralama kayan pencerede değişebilir; `placeholder_id: hmac` önerilir. İstemcinin geri
  gönderdiği literal `<KISI_1>` rezerve edilir (yeni değer `<KISI_2>` alır).
- `response_json_preserve_format` açıkken `scan_response` JSON içerikte de çalışır ama değişiklik olursa JSON yeniden yazılır.
- Analiz CPU işidir ve Python GIL'i altında çalışır; çok yüksek eşzamanlılıkta worker sayısını artırın.
- `--detailed_debug` LiteLLM'in kendisi tarafından guardrail'den önce ham gövdeyi basar; üretimde kapatın.

## Yol haritası

- **Surrogate (sahte değer) modu — v2'ye ertelendi.** Türkçe'de sahte adın ekleri (`Elif'e` ↔ `Ayşe'ye`) her iki yönde
  onarılmalı ve geri çevirmenin tek anlamlı olduğu kanıtlanmalıdır; mevcut yer tutucu + ek onarımı bu riskleri taşımaz.
- Görsel/PDF içi metin (OCR) taraması; Anthropic passthrough akışı (LiteLLM tarafında düzeltme bekleniyor).

## Geliştirme

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev,fast,metrics]"
export MATRIKS_PII_HASH_SECRET=0123456789abcdef0123456789abcdef   # strict örnek config'in selftest'i için
# Windows: set PYTHONUTF8=1 && set MATRIKS_PII_HASH_SECRET=...
pytest -q && pytest -q -m benchmark && pytest -q -m e2e
python scripts/external_bench.py --download && pytest -q -m external
ruff check matriks_pii scripts tests && mypy matriks_pii --ignore-missing-imports
python scripts/corpus_report.py
python -m matriks_pii.selftest --config examples/config.yaml
# sözlükleri yeniden üretmek için:
python scripts/fetch_raw.py && python scripts/build_name_lists.py && python scripts/build_common_words.py && python scripts/build_geo.py
python scripts/build_org_names.py   # isteğe bağlı KAP unvan listesi: scripts/_raw/kap_companies.txt
```

`scripts/build_fixtures.py` `detect/revert/streaming` altındaki üretilmiş dosyaları yeniden yazar; elle yazılmış
dosyalar (`*_ek.yaml`, `*_x.yaml`, `yeni_varliklar.yaml`, `sayisal_ek.yaml`) etkilenmez.

Veri kaynakları ve lisansları: [`matriks_pii/data/LICENSES.md`](matriks_pii/data/LICENSES.md).

## Katkı ve güvenlik

- Katkı kuralları (kurulum, testler, yalnız sentetik veri): [`CONTRIBUTING.md`](CONTRIBUTING.md).
- Davranış kuralları: [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).
- Güvenlik açığı bildirimi (herkese açık issue açmayın): [`SECURITY.md`](SECURITY.md).

## Lisans

Kaynak kod [Apache License 2.0](LICENSE) lisanslıdır, © 2026 Matriks Finansal Teknolojiler A.Ş. (bkz. [`NOTICE`](NOTICE)).

Pakette üçüncü taraf kaynaklardan türetilmiş veri dosyaları bulunur. Ad/soyad listeleri
[mkozturk/trnames](https://github.com/mkozturk/trnames) (MIT), il/ilçe verisi
[ubeydeozdmr/turkiye-api](https://github.com/ubeydeozdmr/turkiye-api) (MIT) kaynaklıdır.
`matriks_pii/data/tr_words.txt`, Hermit Dave'in [FrequencyWords](https://github.com/hermitdave/FrequencyWords)
projesindeki OpenSubtitles 2018 Türkçe sözcük listesinden türetilmiştir ve
[CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/) lisanslıdır; bu dosya Apache-2.0 kapsamında değildir.
Ayrıntılar ve lisans metinleri: [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

**Marka.** Lisans, "Matriks" adını ve logolarını kullanma hakkı vermez (Apache-2.0 m.6). Yazılımın değiştirilmiş
sürümleri başka bir adla dağıtılmalıdır; "Matriks" adı yalnız yazılımın kaynağını belirtmek için kullanılabilir.

**Garanti reddi.** Yazılım, lisansın 7. ve 8. maddeleri uyarınca "OLDUĞU GİBİ" sunulur; her kişisel verinin tespit
edileceği garanti edilmez.
