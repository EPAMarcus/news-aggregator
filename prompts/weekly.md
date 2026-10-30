Write this week's news digest from the daily digests in the data block.

Each daily digest is a `<document>` with a `level` attribute (`daily`) and a `period` attribute,
the day it covers (`YYYY-MM-DD`). The daily digests are already grouped into the categories below.

Rules:

1. Group the digest into sections. Each section starts with a `## ` heading that is exactly one of
   these category names, written exactly like this: $categories
   Use each category at most once, in the order listed, and leave out categories with no news.
   Never invent other top-level categories.
2. A category with several developments on a clear sub-topic may use `### ` sub-sections (for
   example `### AI` under `## Tech`). Sub-section names are free, short and in title case.
3. Write at most $max_words words in total, headings not counted. This is a hard limit. Select the
   most significant developments of the week, and prefer stories that developed over several days
   to one-off items. Leave the rest out.
4. Under each heading, write a few `- ` bullets of one or two sentences each, never nested, or one
   short paragraph.
5. Write concise, high-level English about what happened and why it matters. When several days
   report the same story, describe it once. No per-article detail, no source names, no links, no
   URLs, no citations.
6. Output only the sections. No title, no introduction, no closing remarks, no `Sources` section.
