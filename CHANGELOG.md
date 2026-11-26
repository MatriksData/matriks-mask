# Değişiklik günlüğü / Changelog

## 0.4.2 — 2026-11-26

Guardrail'in model düzeyinde (`litellm_params.guardrails`) tanımlandığı kurulumlar için iki düzeltme.
/ Two fixes for deployments where the guardrail is also attached at model level.

- **Güvenlik — önbellek üzerinden başka kullanıcının verisi:** guardrail bir modelin `litellm_params.guardrails`
  listesinde tanımlıyken LiteLLM yanıt önbelleği (`cache: true`) de açıksa, LiteLLM geri çevirmeyi yanıtı önbelleğe
  yazmadan önce çalıştırır; önbelleğe gerçek değerli yanıt girerdi. Önbellek anahtarı maskeli istekten üretildiği için
  (`Ben <KISI_1>, TCKN <TCKN_1>` herkes için aynıdır) aynı maskeli isteği gönderen başka bir kullanıcı bu değerleri
  alabiliyordu (akışsız istekler). Artık model düzeyinde geri çevrilen yanıt `no-store` işaretlenir ve önbelleğe
  yazılmaz (`deployment_revert_no_store` sayacı, ilk seferde uyarı logu). Yalnız `default_on` ya da istek/anahtar düzeyi
  tanım etkilenmez; önbellek orada çalışmaya devam eder. **Yükseltme sonrası:** model düzeyi tanımı kaldırın ve LiteLLM
  önbelleğini temizleyin ya da önbellek süresinin dolmasını bekleyin (eski kayıtlar gerçek değer taşıyabilir).
  / Security: with the guardrail attached at model level and LiteLLM response caching enabled, the reverted response
  (real values) was written to the cache and could be served to another user sending the same masked request. Such
  responses are now marked `no-store`. After upgrading, remove the model-level attachment and flush the LiteLLM cache.
- **Tekrarlanan post_call (500 `matriks_pii_session_missing`):** guardrail hem `default_on: true` hem de bir modelin
  `litellm_params.guardrails` listesinde tanımlıysa LiteLLM aynı yanıt için `post_call` kancasını iki kez çağırır. İlk
  çağrı yanıtı geri çevirip oturumu siliyor, ikincisi "oturum kayıp" diye isteği 500 ile reddediyordu (kişisel veri
  içeren her akışsız istek). Artık geri çevrilmiş oturum kısa süre hatırlanır; tekrar çağrı, yanıtta yer tutucu
  kalmamışsa işlem yapmadan geçer (`post_call_repeat` sayacı, ilk seferde uyarı logu). Yanıtta yer tutucu (kaçışlı
  yazımlar dâhil) varsa eskisi gibi fail-closed. Çift tanım yine de kaldırılmalıdır.
  / A repeated `post_call` for an already reverted response is now a no-op instead of a 500.

## 0.4.1 — 2026-11-23

Dış değerlendirmenin ikinci raporundan çıkan düzeltmeler. / Fixes from the second external evaluation.

- **Ad:** GitHub deposu ve komut `matriks-mask` (`matriks-mask "metin"` alt komutsuz da maskeler). Python modülü
  (`matriks_pii`), guardrail yolu, `MATRIKS_PII_*` değişkenleri ve metrik adları değişmedi; `matriks-pii` komutu da kurulur.
- **Kısmi maskeleme (sızıntı):** bağlamlı kimliklerde tire/eğik çizgiyle bitişik parçalar artık değerle birlikte
  maskelenir (`Müşteri no 4455-667788`, `Poliçe no …-7`, `MKK sicil no …-8`, `hesap no 0012345/678`); tarih biçimindeki
  değer kimlik sayılmaz (`Poliçe no 1234567, 2024-01-15 tarihli`).
- **Adlar (sızıntı):** BÜYÜK harfli, aynı zamanda sözcük olan soyadlar (`İrem DUMAN`, `KORKMAZ`, `AVCI`, `ÇİFTÇİ`),
  tırnaklı e-posta başlığı (`To: "İrem DUMAN" <…>`) ve ad ile soyadı yer adı da olan kişiler (`Levent Bozkurt`,
  `Defne Demirci`) yakalanır; ilçe + il dizisi (`Çan Çanakkale`) kişi sayılmaz.
- **Yanlış pozitif:** kişi olmayan şeylerin adı (`Ürün Adı:`, `Proje Adı:`, `Şube Adı:`, `Cihaz Adı:`,
  `Kullanıcı Adı:`) kişi alanı sayılmaz; "Adı" sözcüğü maskelenmez. Bu yüzden `Kullanıcı adı: Ayşe` gibi alanda
  tek ad artık maskelenmez (kullanıcı adları için `extra_regex`, bkz. `examples/sektor/finans.yaml`). Yer adı
  dizileri (`Konya Akşehir`, `Kartal Pendik arası`) kişi sayılmaz.
- **Köşeli parantez:** `Ayşe Demir <ayse@example.com>` yanıtta parantezleriyle geri çevrilir (oturum, değerin istekte
  parantez içinde olduğunu tutar; Redis'te geriye uyumlu).
- **Form kodlaması:** URL/form parçasında harf/rakam arasındaki `+` boşluk olarak çözülür (`tel=0532+123+45+67`).
- **strict tarih:** fatura, sözleşme, vade, sipariş, ödeme… etiketli tarihler doğum tarihi sayılmaz.
- `examples/sektor/finans.yaml`: kullanıcı adı `user=MTX\ademir`, JSON (`"username": …`) ve XML (`<UserName>`) biçimleri.

## 0.4.0 — 2026-11-23

İlk PyPI sürümü. / First release on PyPI.

- **PyPI paket adı `matriks-mask`** (`pip install matriks-mask`). Python modülü (`matriks_pii`), guardrail yolu
  (`matriks_pii.guardrail.TurkishPIIGuardrail`), ortam değişkenleri (`MATRIKS_PII_*`) ve metrik adları değişmedi.
  Eski `matriks-pii` kurulu bir ortamda **önce `pip uninstall matriks-pii`**, sonra `pip install matriks-mask`
  (iki dağıtım aynı dosyaları sahiplenir; sonradan eskisini kaldırmak yeni kurulumu bozar).
- LiteLLM guardrail'i: istek sağlayıcıya gitmeden önce kişisel verileri yer tutucularla maskeler, yanıtta (akış dâhil)
  geri çevirir; ünlü uyumuna göre Türkçe ek onarımı.
- 34 temel varlık (TCKN, VKN, IBAN, Troy dâhil kartlar, telefon, plaka, MERSİS, adres, ad-soyad, gizli anahtarlar…;
  kontrol basamağı ve bağlam doğrulamalı) + KVKK m.6'daki 12 özel nitelikli veri kategorisi (kişiye bağlılık şartıyla).
- `balanced` / `strict` / `minimal` profilleri; `extra_regex`, `allow_list`, `deny_list`; isteğe bağlı Türkçe NER.
- Chat/Completions, Embeddings, Responses API, Anthropic Messages, Gemini native; tool çağrıları ve JSON içerik.
- Fail-closed varsayılanlar, değersiz loglama ve denetim izi, Prometheus metrikleri, AES-GCM şifreli Redis oturum
  deposu, kurulum doğrulaması (`selftest`).
- `matriks-pii` komutu (diğer adı `matriks-mask`): `mask` (LiteLLM olmadan maskeleme; `--profile`, `--config`,
  `--json`), `selftest`, `keys`.
- Kuruma özel tanımlar: README'de ayrı bölüm; `examples/sektor/` altında finans, havayolu, sağlık, sigorta, sanayi,
  e-ticaret/kargo, telekom ve yurt dışı örnek dosyaları, her biri kendi ölçüm kümesiyle test edilir.
- Lisans: kod Apache-2.0; paketteki üçüncü taraf veri dosyaları için `THIRD_PARTY_NOTICES.md`.

İç sürümler (0.1.0 – 0.3.3) ayrı paket olarak yayımlanmadı; içerikleri bu sürümdedir. GitHub'daki 0.3.3'e göre
değişenler:

- URL kodlu (`ayse%40example.com`, `0532%20123…`; Outlook SafeLinks gibi) ve HTML varlıklı (`&#64;`) değerler çözülmüş
  bir kopyada taranır, orijinal kodlu aralık maskelenir; yanıtta orijinal kodlamayla geri çevrilir. Türkçe yüzde yazımı
  ("%40 arttı") çözülmez. Kodsuz metinde tespit değişmez.
- XML/HTML etiketli parola (`<Password>…</Password>`, `<şifre>`, `<pwd>`) `SIFRE` olarak yakalanır.
- Tireli hesap no (`0012345-678`) artık tek değer olarak maskelenir (önceden `-678` açıkta kalıyordu).
- `strict` profilde bağlamsız tarih kuralı zaman damgalarını (`2026-11-20 14:32`, `…T09:15`) ve gelecekteki tarihleri
  doğum tarihi saymaz (log trafiğinde yanlış pozitif).
- `extra_regex` için `group` alanı (yalnız yakalama grubu maskelenir). `context` artık büyük/küçük harf ve Türkçe harf
  duyarsızdır (önceden `reçete`, `PNR` gibi bağlamlar katlanmış metinde hiç eşleşmiyordu). `extra_regex` kullanan
  config'lerde `config_hash` değişir.
- `selftest --corpus` hatalı girdide yığın izi yerine biçimi anlatan kısa bir ileti verir (çıkış kodu 2).
- Tek sayıdan oluşan tool argümanında (`"arguments": "1.52e+274"`) sayı içindeki değer maskelenince JSON bozuluyordu;
  artık JSON metni olarak geçerli kalır (özellik testi buldu).
- `constraints/prod.txt`: LiteLLM 1.110.2, fastapi 0.162.5, pydantic 2.14.4 (birim, uçtan uca, selftest, pip-audit ile
  sınandı); CI matrisine LiteLLM 1.110.2 eklendi; GitHub Actions sürümleri (checkout v7, setup-python v7,
  upload-artifact v7, download-artifact v8). Asgari LiteLLM sürümü değişmedi (1.106.1).
- Sektör örneklerine sigorta eklendi (hasar dosya no, ekspertiz rapor no).
- Bölünmez/dar boşluk (U+00A0, U+202F …) ve Unicode tire (U+2010–2013, U+2212) ile yazılmış IBAN, kart, TCKN gibi
  değerler artık yakalanır (web/PDF kopyaları; `&nbsp;`, `%C2%A0`, `&#8209;` gibi kodlu biçimler dahil). Unicode
  tireli yazım ek bir taramayla bulunur; değere bitişik tire ("…7216–CVV") önceki tespitleri etkilemez.
- Değere bitişik ASCII tire (`4111111111111111-x`, `-4111…`) artık kart numarasının maskelenmesini engellemez.
- XML parola etiketi taramasında ikinci dereceden süre giderildi (çok sayıda etiket + parola ifadesi içeren metin).
