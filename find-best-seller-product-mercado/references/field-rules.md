# Field Rules

Use this checklist for every candidate.

| Field | Evidence source | Rule |
|---|---|---|
| 标题 | Detail page | Use the current H1 title. |
| 链接 | Detail page | Keep the canonical Mercado Libre product URL; strip tracking fragments and query parameters. |
| 是否 Ad | Search card | `是` when the card has an explicit sponsored label or its own link contains `is_advertising=true` or `type=pad`. Never infer from position alone. |
| 价格 | Detail page | Use current displayed MXN price, not struck-through list price or installment amount. |
| vendidos 数 | Detail page | Copy the displayed product-level sales string. Missing means reject. |
| 评价数 | Detail page | Use the count attached to the product rating/reviews, not seller reputation. |
| 评分 | Detail page | Use the product rating, normally on a 5-point scale. |
| 场景 | Search-card main image or detail main image | Classify the visual setting, not the product use occasion. |

## Similarity gate

All answers must be yes:

1. Is it the same garment category and set composition?
2. Does it preserve the defining silhouette or construction?
3. Is the main product image clear enough to compare visually?
4. Does the detail page display at least 500 product sales?

## Normalized internal record

Keep this shape while browsing, even if the final answer is a Markdown table:

```json
{
  "title": "",
  "url": "",
  "is_ad": false,
  "price_mxn_display": "",
  "sold_display": "",
  "review_count_display": "未显示",
  "rating_display": "未显示",
  "scene": "",
  "source_query": "",
  "search_position": 0,
  "item_id": "",
  "category_match": true,
  "image_clear": true
}
```

Use `source_query`, `search_position`, `item_id`, `category_match`, and `image_clear` for validation and sorting; they are not required final columns.
