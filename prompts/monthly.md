Write this month's news digest from the digests in the data block.

Each input is a `<document>` with a `level` attribute and a `period` attribute. Weekly digests
(`level="weekly"`, period `YYYY-Www`) cover the weeks that lie fully inside the month. Daily
digests (`level="daily"`, period `YYYY-MM-DD`) cover the remaining days. Together they cover the
month without overlap. All of them are already grouped into the categories below.

Rules:

1. Group the digest into sections. Each section starts with a `## ` heading that is exactly one of
   these category names, written exactly like this: $categories
   Use each category at most once, in the order listed, and leave out categories with no news.
   Never invent other top-level categories.
2. A category with several developments on a clear sub-topic may use `### ` sub-sections (for
   example `### AI` under `## Tech`). Sub-section names are free, short and in title case.
3. Write about $target_words words in total, about one page, and never more than $max_words,
   headings not counted. Focus on the main developments and trends of the month and on stories
   that ran over several weeks. Leave out minor items.
4. Under each heading, write a few `- ` bullets of one or two sentences each, never nested, or one
   or two short paragraphs.
5. Write concise, high-level English about what happened and why it matters. When several inputs
   report the same story, describe it once. No per-article detail, no source names, no links, no
   URLs, no citations.
6. Output only the sections. No title, no introduction, no closing remarks, no `Sources` section.
