# MetricsSpecification&ReportingGuide.md (FULL, synced with history_metrics.csv)

Цей документ **повністю** описує всі метрики, які зберігаються у `history_metrics.csv`, включно з усіма breakdown-полями
(video/banner, all/unique, %, time, cost, debug). Назви полів = назви колонок у CSV та полів `RunMetrics`.

---

## 1) Описові змінні

- `report_date` — дата репорту (YYYY-MM-DD).
- `generated_at` — час генерації репорту.
- `report_type` — тип репорту (daily/weekly тощо).
- `stage` — номер етапу (1 — базовий, 2+ — дообробка/історія).

---

## 2) Total (вхідні лінки)

- `total_links_all` — загальна кількість рядків/лінків у links_df.
- `total_links_unique` — кількість унікальних лінків.

---

## 3) Identified (успішно завантажені, IsDownload == 200)

### 3.1 Загальні
- `identified_all` — кількість identified (усі).
- `identified_unique` — identified (унікальні).
- `identified_all_pct_of_all` — % identified_all від total_links_all.
- `identified_unique_pct_of_unique` — % identified_unique від total_links_unique.

### 3.2 Розбиття за типами
- `identified_video_all` — identified відео (усі).
- `identified_video_unique` — identified відео (унікальні).
- `identified_banner_all` — identified банери (усі).
- `identified_banner_unique` — identified банери (унікальні).

### 3.3 Відсотки типів
- `identified_video_all_pct_of_identified` — % identified_video_all від identified_all.
- `identified_banner_all_pct_of_identified` — % identified_banner_all від identified_all.
- `identified_video_unique_pct_of_identified_unique` — % identified_video_unique від identified_unique.
- `identified_banner_unique_pct_of_identified_unique` — % identified_banner_unique від identified_unique.

---

## 4) Classified via API (фактично пройшли через API класифікації)

> **UI “фактично оброблялися”** має відповідати саме цим метрикам.

### 4.1 Загальні
- `classified_api_all` — класифіковані через API (усі).
- `classified_api_unique` — класифіковані через API (унікальні).
- `classified_api_all_pct_of_identified` — % classified_api_all від identified_all.
- `classified_api_unique_pct_of_identified_unique` — % classified_api_unique від identified_unique.

### 4.2 Розбиття за типами
- `classified_api_video_all` — API відео (усі).
- `classified_api_video_unique` — API відео (унікальні).
- `classified_api_banner_all` — API банери (усі).
- `classified_api_banner_unique` — API банери (унікальні).

### 4.3 Відсотки типів
- `classified_api_video_all_pct_of_identified_video` — % classified_api_video_all від identified_video_all.
- `classified_api_video_unique_pct_of_identified_video_unique` — % classified_api_video_unique від identified_video_unique.
- `classified_api_banner_all_pct_of_identified_banner` — % classified_api_banner_all від identified_banner_all.
- `classified_api_banner_unique_pct_of_identified_banner_unique` — % classified_api_banner_unique від identified_banner_unique.

---

## 5) Correctly classified (під історію)

“Правильно класифіковані” — ті, що підлягають запису в історію.
Критерій: `brand` і `product` не можуть бути одночасно порожні/“-”/“not found”.

### 5.1 Загальні
- `classified_correct_all` — правильно класифіковані (усі).
- `classified_correct_unique` — правильно класифіковані (унікальні).
- `classified_correct_all_pct_of_identified` — % classified_correct_all від identified_all.
- `classified_correct_unique_pct_of_identified_unique` — % classified_correct_unique від identified_unique.

### 5.2 Розбиття за типами
- `classified_correct_video_all` — правильно відео (усі).
- `classified_correct_video_unique` — правильно відео (унікальні).
- `classified_correct_banner_all` — правильно банери (усі).
- `classified_correct_banner_unique` — правильно банери (унікальні).

### 5.3 Відсотки типів
- `classified_correct_video_all_pct_of_identified_video` — % classified_correct_video_all від identified_video_all.
- `classified_correct_video_unique_pct_of_identified_video_unique` — % classified_correct_video_unique від identified_video_unique.
- `classified_correct_banner_all_pct_of_identified_banner` — % classified_correct_banner_all від identified_banner_all.
- `classified_correct_banner_unique_pct_of_identified_banner_unique` — % classified_correct_banner_unique від identified_banner_unique.

---

## 6) History taken (взято ДО API)

Класифікація взята з історії (без виклику API).

### 6.1 Загальні
- `hist_taken_all` — взято з історії (усі).
- `hist_taken_unique` — взято з історії (унікальні).

### 6.2 Типи (all)
- `hist_taken_video_all` — взято з історії відео (усі).
- `hist_taken_banner_all` — взято з історії банери (усі).

### 6.3 Відсоток від пулу
Пул = історія + правильно класифіковані (історія береться “до API”).

- `hist_taken_pct_of_pool`
- `hist_taken_video_pct_of_pool`
- `hist_taken_banner_pct_of_pool`

---

## 7) Broken download (пошкоджені/не валідні для завантаження)

- `broken_links_all` — пошкоджені (усі) на етапі download.
- `broken_links_unique` — пошкоджені (унікальні) на етапі download.

---

## 8) Time metrics

### 8.1 Upload (download) time
- `upload_time_total_creatives_sec`
- `upload_time_avg_creative_sec`
- `upload_time_total_video_sec`
- `upload_time_avg_video_sec`
- `upload_time_total_banner_sec`
- `upload_time_avg_banner_sec`

### 8.2 Classification (API) time
- `classify_time_total_creatives_sec`
- `classify_time_avg_creative_sec`
- `classify_time_total_video_sec`
- `classify_time_avg_video_sec`
- `classify_time_total_banner_sec`
- `classify_time_avg_banner_sec`

### 8.3 Pipeline time (паралельна обробка, не сума upload+classify)
- `pipeline_time_total_creatives_sec`
- `pipeline_time_avg_creative_sec`
- `pipeline_time_total_video_sec`
- `pipeline_time_avg_video_sec`
- `pipeline_time_total_banner_sec`
- `pipeline_time_avg_banner_sec`

---

## 9) Tokens (для трекінгу витрат)

- `img_tok_in` — вхідні токени по image.
- `img_tok_out` — вихідні токени по image.
- `vid_tok_in` — вхідні токени по video.
- `vid_tok_out` — вихідні токени по video.

---

## 10) Cost (USD)

- `img_cost_usd` — витрати по image (USD).
- `vid_cost_usd` — витрати по video (USD).
- `total_cost_usd` — сумарні витрати (USD).

---

## 11) Debug / Tech

Ці поля потрібні для діагностики та валідності підрахунків.

- `img_api_rows` — кількість рядків, що пішли в API по images.
- `vid_api_rows` — кількість рядків, що пішли в API по videos.
- `img_dups` — дублікати (images).
- `vid_dups` — дублікати (videos).
- `classify_broken_banner_all` — пошкоджені банери у класифікації (pipeline).
- `classify_broken_video_all` — пошкоджені відео у класифікації (pipeline).
- `classify_broken_creatives_all` — загалом пошкоджені у класифікації (pipeline).

---

## 12) Примітка про Delta (UI)

`history_metrics.csv` зберігає **сирі метрики кожного етапу**.
“Покращення / Δ Correct(...)” у UI — це похідні значення, які рахуються з двох рядків CSV (етап N і етап N-1),
на базі:

- приріст: `classified_correct_all(stage N) - classified_correct_all(stage N-1)`
- % приросту: від `classified_correct_all(stage N-1)`

