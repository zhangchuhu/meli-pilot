# SHEIN field and evidence rules

Read this file before collecting or reporting candidates.

## Evidence hierarchy

Prefer evidence in this order:

1. the candidate's current SHEIN product page;
2. a product-specific SHEIN search card from the current run;
3. structured product data embedded in the same current SHEIN page.

Do not use third-party snippets, guessed values, or unrelated variants. Record the evidence source internally for every threshold field.

## Field rules

- **标题**: Use the current SHEIN product title.
- **链接**: Use the canonical `us.shein.com` product URL without search or tracking parameters when possible.
- **是否 Ad**: `是` only when the originating search placement explicitly says `Sponsored`, `Ad`, or an equivalent SHEIN label. Use `否` when the placement is demonstrably organic. Use `未标注` when the originating placement cannot be inspected. Position alone is not evidence.
- **价格**: Use the current displayed US-site selling price and preserve its currency. Do not silently replace it with a crossed-out list price.
- **vendidos 数**: Use product-specific sold text, such as `1k+ sold`, from the current product page or its current search card. Normalize `k` as 1,000 and `m` as 1,000,000 for threshold comparison. A time-bounded figure qualifies only by its displayed numeric amount. `Popular`, `Best Seller`, stock status, review count, and cart count are not sold counts.
- **评价数**: Use the total product review count, not the number of reviews currently loaded or the count for one star bucket.
- **评分**: Use the product's current numeric rating. Do not derive it from star graphics unless the page exposes an accessible numeric value.
- **场景**: Infer conservatively from garment design and product imagery. Prefer one or two labels: 日常、通勤、约会、派对、晚宴、度假、海滩、婚礼宾客、街头、运动、居家. Mark it as `推断` when the page does not state a use case.

## Qualification

All three conditions must be true for the same product:

| Metric | Minimum |
| --- | ---: |
| vendidos | 500 |
| reviews | 50 |
| rating | 4.1 |

Examples:

- `500+ sold`, 86 reviews, 4.6 → qualifies.
- `400+ sold`, 2,000 reviews, 4.9 → fails sold threshold.
- `1k+ sold`, no review count, 4.7 → fails because evidence is missing.
- 700 reviews and 4.8 with no sold value → fails; reviews are not sales.

## Ranking and deduplication

Track the earliest visible position across all queries. Rank qualifying organic candidates by earliest organic position, then use qualifying sponsored candidates only to fill remaining slots. Deduplicate color/size URLs that resolve to the same SHEIN goods ID unless they are genuinely separate product listings.

## Image quality

A candidate image must make the garment type, silhouette, and defining details legible. Reject broken images, thumbnails too small to verify, severe crop or obstruction, unrelated collages, and obvious category mismatches.

## Audit record

For each reported row, retain during the run:

```text
query | search_position | ad_label_evidence | canonical_url
category_match | image_quality | sold_text | sold_normalized
review_text | reviews_normalized | rating_text | rating_normalized
price_text | scene | evidence_source
```
