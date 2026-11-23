---
title: Showcase
author: md-html-builder
description: Every Markdown feature supported by md-html-builder.
order: 1
---

# Markdown showcase

![md2html logo](images/logo.svg "The md2html logo")

This page exercises **every** Markdown feature supported by *md-html-builder*. Visit the
[getting started guide](guide/getting-started.md) for usage.

## Text formatting

Plain text, *italic*, **bold**, ***bold italic***, ~~strikethrough~~, `inline code`,
and escaped characters: \*not italic\*, 5 < 6 & 7 > 3.

Line one with a hard break  
line two.

## Links

- Inline: [Python](https://www.python.org "Python homepage")
- Reference: [CommonMark][cm]
- Autolinks: <https://github.com> and bare www.example.com or hello@example.com
- Cross-page: [guide](guide/getting-started.md#installation)

[cm]: https://commonmark.org

## Lists

1. First
2. Second
   - Nested bullet
     1. Deeply nested number
3. Third

- [x] Write the parser
- [x] Add syntax highlighting
- [ ] Take over the world

Term
: A definition list entry.

## Blockquotes and alerts

> A quote.
>
> > A nested quote.

> [!NOTE]
> GitHub-style alerts are supported.

> [!WARNING]
> Be careful out there.

## Tables

| Feature    | Supported | Notes                 |
|:-----------|:---------:|----------------------:|
| Tables     | yes       | with alignment        |
| `code`     | yes       | inline **formatting** |
| Pipes \| … | yes       | escaped               |

## Code

```python
from dataclasses import dataclass


@dataclass
class Greeter:
    """Say hello."""

    name: str

    def greet(self) -> str:
        return f'Hello, {self.name}!'
```

```javascript
const add = (a, b) => a + b;
console.log(`2 + 3 = ${add(2, 3)}`);
```

```bash
md2html docs/ -o site/ --toc left --page-numbers
```

```json
{"theme": "default", "mode": "auto"}
```

```unknown-language
Unknown languages fall back to plain text.
```

    Indented code blocks work too.

## Math

Inline math $E = mc^2$ and a block:

$$
\int_0^1 x^2 \, dx = \frac{1}{3}
$$

## Footnotes

Here is a footnote reference[^1] and another[^long].

[^1]: The footnote text.
[^long]: Footnotes can contain *formatting* too.

## Raw HTML

<details>
<summary>Click to expand</summary>

Raw HTML passes through unless safe mode is enabled.

</details>

---

The end.
