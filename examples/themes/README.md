# Theme gallery

Each page renders `examples/docs/index.md` (every supported Markdown feature) with one
built-in theme, in the default **paged** view (the document shown as an A4 sheet, exactly
as it prints or exports to PDF), with a header, footer, page numbers and a right-hand TOC.
Images are embedded, so every file is self-contained.

| File | Theme | Look |
|---|---|---|
| `default.html` | `default` | Clean modern sans-serif, blue accent, light/dark |
| `academic.html` | `academic` | Serif journal style on warm paper, justified text, booktabs tables |
| `midnight.html` | `midnight` | Dark in every mode, luminous blue/violet accents |
| `minimal.html` | `minimal` | Monochrome and airy; colour only in code |
| `default-web.html` | `default` | The same page in the classic `web` view with a left TOC |
| `academic.pdf` | `academic` | The `academic` page exported straight to PDF |

Regenerate (from the project root):

```bash
for t in default academic midnight minimal; do
  md2html examples/docs/index.md -o examples/themes/$t.html --theme $t \
    --title md-html-builder --header-template '<strong>{site_title}</strong> &middot; {title}' \
    --footer-template '{title} &middot; {date}' --page-numbers --toc right --assets embed
done
md2html examples/docs/index.md -o examples/themes/default-web.html --view web --toc left \
  --title md-html-builder --header-template '<strong>{site_title}</strong> &middot; {title}' \
  --footer-template '{title} &middot; {date}' --page-numbers --assets embed
md2html examples/docs/index.md -o examples/themes/academic.pdf --theme academic \
  --title md-html-builder --header-template '<strong>{site_title}</strong> &middot; {title}' \
  --footer-template '{title} &middot; {date}' --page-numbers
```

Open any page in a browser and use Print > Save as PDF: the sheet becomes real pages with
the header in the top margin and the footer and "Page N of M" in the bottom margin.
