---
title: Getting started
order: 2
---

# Getting started

## Installation

```bash
pip install -e .
```

## Convert a file

```bash
md2html README.md
```

## Convert a directory

```bash
md2html docs/ -o site/ -c md2html.yaml
```

## Make a PDF

```bash
md2html README.md -o README.pdf          # PDF only
md2html docs/ -o site/ --format both     # HTML pages plus a PDF of each
```

Back to the [showcase](../index.md).
