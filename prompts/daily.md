Write today's daily news digest from the news items in the data block.

Each item is a `<document>` with an `index` number, a `source` attribute and, for most sources,
a `topic` attribute such as `Tech` or `Politics`. The topic is a strong hint for where the item
belongs. Follow it unless the content clearly belongs to another category, for example a general
news site covering many subjects. Items without a topic are placed by their content alone.

Rules:

1. Group the digest into sections. Each section starts with a `## ` heading that is exactly one of
   these category names, written exactly like this: $categories
   Use each category at most once, in the order listed, and leave out categories with no news.
   Never invent other top-level categories. An item that fits none of them goes under `Other` if
   it is listed, otherwise under the closest category.
2. A category with several items on a clear sub-topic may use `### ` sub-sections (for example
   `### AI` under `## Tech`). Sub-section names are free, short and in title case. Do not use
   sub-sections for one or two bullets.
3. Write at most $max_bullets bullets in total, across all sections. This is a hard limit. Select
   the most significant news and leave the rest out. Bullets are single-level `- ` list items,
   never nested.
4. When several items report the same story, merge them into one bullet.
5. Each bullet is one or two concise, high-level English sentences about what happened and why it
   matters. No per-article detail, no source names, no links, no URLs.
6. End every bullet with the `index` numbers of all documents it is based on, in one pair of
   square brackets: `[3]`, or `[3, 7]` for a merged story. Cite only documents that report the
   story. The links to the articles are added from these numbers.
7. Output only the sections. No title, no introduction, no closing remarks, no `Sources` section:
   the list of sources is added separately.
