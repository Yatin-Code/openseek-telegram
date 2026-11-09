---
name: OpenSeek dashboard
description: The original OpenSeek desktop visual system carried into the shared browser dashboard.
colors:
  background: "#0d0e12"
  surface: "#13151b"
  card: "#181b23"
  card-high: "#1f2330"
  border: "#252a36"
  text: "#e9eaf0"
  muted: "#8b91a1"
  accent: "#8b7bff"
  teal: "#5ad1c1"
  good: "#4fd68a"
  warning: "#f0b44c"
  danger: "#ef6a6a"
typography:
  body:
    fontFamily: '"Segoe UI Variable Text", "Segoe UI", system-ui, sans-serif'
    fontSize: "14px"
    lineHeight: 1.45
  heading:
    fontSize: "22px"
    fontWeight: 600
    lineHeight: 1.2
  metric:
    fontSize: "26px"
    fontWeight: 600
    lineHeight: 1.15
  label:
    fontSize: "13px"
  status:
    fontSize: "12px"
  code:
    fontFamily: 'ui-monospace, "Cascadia Mono", SFMono-Regular, Menlo, Consolas, monospace'
    fontSize: "13px"
rounded:
  control: "9px"
  panel: "14px"
  preview: "12px"
  pill: "10px"
  chip: "8px"
  focus: "6px"
  progress: "4px"
spacing:
  gap: "14px"
  main: "28px 32px 24px"
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.background}"
    rounded: "{rounded.control}"
    padding: "10px 16px"
    height: "44px"
  panel:
    backgroundColor: "{colors.card}"
    textColor: "{colors.text}"
    rounded: "{rounded.panel}"
    padding: "20px 18px"
---

# Design System: OpenSeek

## Overview

The visual authority is the original OpenSeek desktop application: `desktop/openseek_desktop/theme.py` and `ui.py` at commit `9691f9a`, matching the upstream version recorded in `UPSTREAMS.md`. The browser uses its dark sidebar, violet actions, teal highlights, restrained metrics, and bordered panels. This preserves the user's chosen OpenSeek appearance while displaying the current NVMT pool features.

The implementation lives in `backend/openseek/dashboard.py`. Local, VPS, and Vercel use this same dashboard. Vercel's environment setup page uses the same palette.

## Colors

Violet identifies primary actions and focus. Teal identifies connection information. Green, amber, and red supplement readable status labels. Dark tonal surfaces and a single fine border separate areas. Muted text remains readable; status must never rely on color alone.

Interaction shades preserve the original theme: primary hover `#9d8fff`, secondary hover `#232838` with `#3a4155` borders, disabled primary `#2b2846`, and destructive surfaces `#2a1d22` with `#5a2c33` borders. Translucent status backgrounds use the corresponding semantic color. The brand gradient and white switch thumb are deliberate component details.

## Typography

Use the installed Segoe UI/system stack. Page headings, metric values, panel headings, and supporting text have distinct sizes and weights. Codes and fingerprints use monospace only where exact copying or comparison matters. Long addresses and identifiers wrap without widening the page.

Component exceptions include 11px metadata chips, 15px branding, 20px phone headings, and 24px pairing codes. The standalone Vercel setup page uses rem equivalents and its platform font fallback; no remote font is loaded.

## Layout

The desktop layout has a 250px sidebar and a separately scrolling main area. Keep the sidebar available while long forms or tables scroll. At 900px and below, navigation becomes a horizontal row and forms use one column. At 520px and below, metrics stack. Device and invitation tables become labelled rows on narrow screens.

Use the existing six destinations: Dashboard, Library, Live activity, Diagnostics, Sharing, and Settings. Put each new owner feature in the appropriate destination. Do not create a separate unstyled admin page for it.

## Elevation & Depth

Use tonal surface changes and fine borders. Panels do not use decorative shadows or glow. Status pills and selected navigation use a higher surface tone.

## Shapes

Panels have soft, consistent corners; controls use the tighter control radius. Preview frames preserve their landscape aspect ratio. They are actual NVMT thumbnails, not cropped poster substitutes.

## Components

Buttons have a usable 44px minimum height, visible keyboard focus, and meaningful busy and disabled states. Labels sit beside or above their controls. The primary action uses violet; destructive actions use explicit red text and a confirmation step where needed.

Forms retain drafts, focus, scroll position, and validation messages during polling. Lists retain expanded details and confirmations. Controls report success only after a successful backend response. Loading, empty, unsupported, and failure states explain what the user can do next.

The library loads bounded pages from the existing index and fetches previews using the admin header, then displays blob URLs. Dashboard metrics come from actual pool/cache/job responses. Diagnostics shows actual status rather than an invented log feed.

## Do's and Don'ts

- Do integrate new owner actions into the shared dashboard and reuse existing backend services.
- Do use the original OpenSeek tokens and navigation for new screens and states.
- Do test desktop and narrow screens, long text, keyboard focus, busy states, and polling with a draft in progress.
- Do show profile limitations where they affect an action.
- Don't replace the backend to change the UI, add fake metrics, or resurrect retired desktop process controls.
- Don't put secrets in image URLs, persist media URLs, or load external fonts, scripts, or frameworks.
