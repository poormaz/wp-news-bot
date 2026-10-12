# Featured images

## Audit

**What happened in the live samples run (GitHub Actions run 37851559690).** All four articles
had `media_id = 0`, which means no featured image. The safe policy found none of its three
allowed sources:

* no source in `sources.yaml` is marked `images: press_kit`;
* none of the four games has an entry with `featured_media` in `localization_pages.yaml`;
* `NEWSBOT_FALLBACK_MEDIA_ID` was not set.

**Bot v1 behaviour.** v1 copied the source article's `og:image` into the Poormaz media library
for every post (5 of 5 in runs #2140–#2145, see `docs/COMPARISON.md`). The rights to those
images were never established. v2 never copies publisher images unless someone explicitly
switches `NEWSBOT_IMAGE_POLICY=legacy`, which is not recommended.

**How the Gamxo theme shows posts without a featured image: not verified yet.**

* poormaz.com cannot be reached from the development environment.
* A web search found no Gamxo documentation that describes this case.
* No theme file was read or changed.

Two read-only ways to find out:

1. **`check` mode** prints `FEATURED_IMAGE_AUDIT_JSON` with:
   * the theme slug(s) referenced by the homepage;
   * which of the 30 most recent posts have no featured image;
   * for one post with an image and one without: the HTTP status, whether the page has an
     `og:image`, the theme's `wp-post-image` element, and a placeholder hint;
   * for each configured fallback ID: whether it exists and is an image.
2. **Draft canary preview.** Open the canary draft with *Preview* to see the single-post
   header, and check the home and category lists after publishing.

Magazine themes usually do one of three things when a post has no featured image:

* drop the header image on the single post;
* show an empty box or a theme placeholder in grids, sliders and "related posts" widgets;
* give social shares no preview image, unless Rank Math has a default OpenGraph image.

## Proposal (update-safe and legally safe)

The recommendation is to use Poormaz's own artwork through WordPress settings only.

1. **Design 4–6 generic Poormaz news covers.** They are made in-house:
   * Poormaz logo, brand colours and neutral abstract gaming motifs;
   * no game logos, key art, screenshots or characters from third parties.

   Use 1200×675 (16:9) JPG or WebP files under about 300 KB. Optionally make a separate set
   for hardware news.
2. **Upload them through *Media → Add New*.** This is the normal WordPress UI, with no theme
   or plugin change. Note each attachment ID, shown in the media URL as `item=<id>`.
3. **Set the repository variable** `NEWSBOT_FALLBACK_MEDIA_IDS`, for example
   `gaming=101,102,103; hardware=201,202; default=101`. The bot picks a cover per story
   (deterministically rotated, so consecutive posts differ). Before every use it confirms the ID
   still exists as an image (`GET /wp/v2/media/{id}`). A deleted or wrong ID is skipped with a
   warning; it never ends up on a post.
4. **Run `check`.** Every configured ID must show `"found": true, "is_image": true`.
5. **Optional: set a Rank Math default image** (*Titles & Meta → Global Meta → OpenGraph
   Thumbnail*) to one of the covers, so social previews always have a Poormaz image. This is a
   plugin setting and survives updates.
6. **Better still: game-specific Poormaz artwork.** Add `featured_media` to the entries in
   `localization_pages.yaml`. `check` lists each discovered localization page with its
   `featured_media`. The bot prefers that artwork over the generic covers, also after
   validating it.

### Why this is safe

* Every image is Poormaz-owned and already in its own media library. It is referenced by ID,
  and nothing is downloaded from third parties.
* The theme, its templates and the plugins stay unchanged, so theme and plugin updates cannot
  undo or break it.
* Each ID is validated on every run.

### Order of choice in code (`newsbot/images.py`, policy `safe`)

1. A press-kit image, only from sources explicitly marked `images: press_kit` in
   `sources.yaml`. Mark a source only if its press terms allow editorial use.
2. The matching localization page's `featured_media` (validated).
3. `NEWSBOT_FALLBACK_MEDIA_IDS` for the article's content type, otherwise `default`
   (validated and rotated).
4. No featured image, so the theme default applies. The report says why, including any
   skipped IDs.

### Not done, and not recommended

* **Copying publisher `og:image`, screenshots or key art.** The rights are not established.
* **Per-article generated title cards** (the headline rendered on a branded background). This
  is possible later with Pillow, an OFL Persian font such as Vazirmatn and RTL shaping.
  It adds a dependency, a font asset and a design review, so it is not part of this change.
