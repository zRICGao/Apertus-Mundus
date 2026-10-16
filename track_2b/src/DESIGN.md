---
name: "Apertus Mundus"
description: "Technical clarity and a quiet literary page for the English GM workbench."
colors:
  shell: "#10253e"
  shell-raised: "#163050"
  shell-deep: "#0b1d32"
  paper: "#f6f5f1"
  ink: "#163050"
  muted-ink: "#566475"
  accent: "#e50611"
  accent-hover: "#c9050f"
  white: "#ffffff"
  text: "#e7edf5"
  text-dim: "#b1bdcc"
  panel-border: "#2b4059"
  control-border: "#42556c"
  danger: "#ff939b"
  ok: "#4fc98a"
  info: "#5b9cf5"
typography:
  brand:
    fontFamily: "Georgia, \"Noto Serif SC\", serif"
    fontSize: "24px"
    fontWeight: 400
    letterSpacing: "-0.02em"
  display:
    fontFamily: "\"Noto Serif SC\", Georgia, serif"
    fontSize: "46px"
    fontWeight: 500
    lineHeight: 1.55
    letterSpacing: "-0.02em"
  headline:
    fontFamily: "\"Noto Serif SC\", Georgia, serif"
    fontSize: "28px"
    fontWeight: 600
    lineHeight: 1.5
    letterSpacing: "0"
  body:
    fontFamily: "\"Noto Serif SC\", serif"
    fontSize: "18px"
    fontWeight: 400
    lineHeight: 2
    letterSpacing: "0.01em"
  control:
    fontFamily: "\"Noto Sans SC\", sans-serif"
    fontSize: "13px"
    fontWeight: 500
    letterSpacing: "0"
  label:
    fontFamily: "\"Noto Sans SC\", \"Segoe UI\", sans-serif"
    fontSize: "12px"
    fontWeight: 500
    letterSpacing: "0.02em"
  mono:
    fontFamily: "\"JetBrains Mono\", monospace"
    fontSize: "12px"
    fontWeight: 600
rounded:
  flat: "0"
  segment: "4px"
  control: "6px"
  dialog: "12px"
spacing:
  6: "6px"
  8: "8px"
  12: "12px"
  16: "16px"
  22: "22px"
  24: "24px"
  36: "36px"
  48: "48px"
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.white}"
    typography: "{typography.control}"
    rounded: "{rounded.control}"
    padding: "8px 16px"
  button-primary-hover:
    backgroundColor: "{colors.accent-hover}"
    textColor: "{colors.white}"
  button-ghost:
    backgroundColor: "transparent"
    textColor: "#dce5f0"
    typography: "{typography.control}"
    rounded: "{rounded.control}"
    padding: "8px 16px"
  button-danger:
    backgroundColor: "rgba(224,92,92,.1)"
    textColor: "{colors.danger}"
    typography: "{typography.control}"
    rounded: "{rounded.control}"
    padding: "8px 16px"
  button-ok:
    backgroundColor: "rgba(79,201,138,.1)"
    textColor: "{colors.ok}"
    typography: "{typography.control}"
    rounded: "{rounded.control}"
    padding: "8px 16px"
  button-info:
    backgroundColor: "rgba(91,156,245,.1)"
    textColor: "{colors.info}"
    typography: "{typography.control}"
    rounded: "{rounded.control}"
    padding: "8px 16px"
  input-dark:
    backgroundColor: "{colors.shell-deep}"
    textColor: "{colors.text}"
    rounded: "{rounded.control}"
    padding: "11px 12px"
  navigation:
    backgroundColor: "{colors.shell}"
    textColor: "{colors.text-dim}"
    padding: "0 18px"
  directory-tab:
    textColor: "{colors.text-dim}"
    rounded: "{rounded.segment}"
    padding: "6px 14px"
  character-row:
    backgroundColor: "transparent"
    rounded: "{rounded.flat}"
    padding: "14px 4px"
  branch-option:
    backgroundColor: "{colors.white}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "12px 14px"
---

# Design System: Apertus Mundus

## Overview

**Creative North Star: "Contemporary Atlas"**

Apertus Mundus pairs a navy instrument frame with a quiet ivory reading page. Technical minimalism lives in compact English controls, thin separators and explicit state changes; light fantasy lives in serif narrative typography and the restrained page-arrival motion.

This specification describes the shipped GM workbench in index.html, with frontend_workbench.css loaded after its base styles, and its device connection extension at /connect (connect.html and device_connect.css). The same GM workbench adapts to mobile screens. The separate phone.html NPC communication page retains its earlier green interface; its added synchronization metadata and disconnected-state handling do not constitute a visual redesign. These tokens do not claim to describe that page.

The connection extension carries the same navy frame, ivory task surface, serif headings and red primary action into a full-page device handoff. Its QR code is functional connection content, not an ornamental motif or a new visual identity. This extension was documented from source only: no new-page browser screenshots were available, so its rendered typography, layout and scanning presentation have not been visually verified.

**Key Characteristics:**

- Navy navigation and sidebars frame an ivory narrative surface.
- Serif story text contrasts with English sans-serif controls and monospaced identifiers.
- Red identifies the primary action, active navigation underline and keyboard focus.
- Rows, fine rules and restrained corners keep the workspace dense without boxing every paragraph.

## Colors

The palette joins an ink-blue operating frame, warm near-white paper and a precise red accent. The frontmatter owns exact primitive values; the roles below describe their use. Sidecar tonal ramps are synthesized panel previews of these hues, not additional shipped CSS tokens.

### Primary

- **Signal Red** (`accent`): primary buttons, brand punctuation, active navigation underline, selection and keyboard focus. `accent-hover` is the darker interactive state.

### Secondary

- **Semantic status colors** (`danger`, `ok`, `info`): retained contextual actions and state feedback in the existing tools. They are functional signals, not additional brand accents. Danger controls on the ivory scene heading use a pale red background and darker text rather than the dark-shell variant.

### Neutral

- **Instrument Navy** (`shell`): header, sidebars and dialogs; **Raised Navy** (`shell-raised`): retained panel contexts; **Deep Navy** (`shell-deep`): outer canvas and dark fields.
- **Ivory Paper** (`paper`): reading and launcher form surfaces; **White** (`white`): light fields, options and primary-button text.
- **Navy Ink** (`ink`) and **Subdued Ink** (`muted-ink`): text on light surfaces. **Shell Text** (`text`) and **Subdued Shell Text** (`text-dim`): text on navy.
- **Panel Rule** (`panel-border`) and **Control Rule** (`control-border`): structural and interactive boundaries. Context-specific rules remain in the CSS; they do not form a single interchangeable border scale.

The connection stylesheet uses local names `navy` and `red` for the existing shell and accent values, with the same paper and ink. Its online indicator uses a contextual dark green (`#1f7252`) beside explicit status text on paper; this does not replace the workbench's status palette.

**The Frame and Page Rule.** Use navy for the operating frame and ivory for the primary narrative reading surface. Keep their text colors paired with their own backgrounds.

## Typography

**Brand Font:** Georgia, then Noto Serif SC, then serif. The header wordmark is regular (24px); the launch wordmark uses weight 500 (25px).

**Heading Font:** Noto Serif SC, then Georgia, then serif. **Narrative Body Font:** Noto Serif SC, then serif, as assigned directly by the base narrative rule. **UI Font:** Noto Sans SC, then Segoe UI, then sans-serif for the shell, labels and navigation. The inherited base button, input and option classes specify Noto Sans SC, then sans-serif directly. **Technical Font:** JetBrains Mono, then monospace.

The prose has book-like leading; interface labels stay compact. The loaded font resources include Noto Serif SC weights 400/600/700, Noto Sans SC 300/400/500/600 and JetBrains Mono 400/500/600/700. The launch display declares serif weight 500; availability and fallback depend on the browser, so this is not evidence of a separately loaded 500 face.

### Hierarchy

- **Display:** launch title, frontmatter display role; becomes 32px at 800px.
- **Headline:** scene title, frontmatter headline role; becomes 25px at 1150px. Launcher form heading is 27px/600, edit-exit heading 24px.
- **Body:** narrative role, max-width 72ch, pre-line whitespace. Becomes 17px at 1000px and line-height 1.95 at 800px.
- **Control:** standard button 13px/500; ordinary fields 14px. Small buttons remain 13px; large buttons use 14px. Compact mobile toolbar controls use 12px.
- **Label:** ordinary metadata uses 12px sans-serif with 0.02em tracking, without forced uppercase. Identifiers and option numbers retain mono.

### Connection page typography

The connection introduction uses the existing heading font stack at 42px/500, line-height 1.6 and tracking -0.02em; its mobile size is 30px. The ivory connection panel heading uses that serif stack at 27px/600 and inherits line-height 1.8. The page wordmark explicitly uses Georgia, serif at 26px, reducing to 22px on mobile. Ordinary page copy uses the UI stack at 15px with line-height 1.8; supporting panel copy is 14px and status/help copy 12–13px. These are page-specific applications, not replacements for the incumbent display or body tokens.

Unlike index.html, connect.html does not load the online Noto font stylesheets. The declared Noto families therefore depend on local availability, and the actual rendered fallback is unverified. Preserve this source-level distinction rather than claiming the same downloaded font faces on both pages.

**The Two Voices Rule.** Use serif type for narrative and scene headings, sans-serif for controls, and mono only for identifiers or technical values.

## Layout

The GM application occupies 100dvh. A header (minimum 66px) and contextual ribbon (minimum 48px) sit above the workspace. Above 800px, the single-scene workspace uses a three-column grid: the reading page caps at 720px, with remaining space shared between the directory and party sidebars (minimum 200px and 224px). The scene title leads a second row containing rollback and investigation controls, directly above the prose. Multi-team workspaces retain their previous flex layout. Header horizontal padding is 22px; the reading column uses 36px horizontal padding. The story, branches and composer scroll as one column, rather than placing a fixed composer over prose.

The launcher uses an asymmetric two-column grid (1.25fr / .75fr with a 360px minimum form column), then stacks at 800px. Its spacing belongs to the entry surface, not every future form.

### Responsive behavior

- At **1600px and above**, reading padding grows to 48px. The single-scene page remains capped at 720px and its sidebars share the remaining width; multi-team sidebars retain 256px / 284px.
- At **1150px and below**, reading padding becomes 24px and connection text is hidden. Single-scene sidebars keep their grid minimums; multi-team sidebars use 200px / 224px.
- At **1000px and below**, narrative type becomes 17px. Multi-team sidebars use 190px / 208px and their columns retain specialized scrolling behavior.
- At **800px and below**, the header wraps (minimum 98px), task navigation gets its own 44px row, the ribbon compacts, and scene/directory/party tabs choose one visible workspace pane. Reading padding is 22px. The composer stacks vertically with a full-width submit action. This is the mobile GM workspace, not phone.html.
- The retained **640px** base breakpoint stacks advanced modal columns and adjusts toast wrapping; the later workbench stylesheet overrides its old workspace stacking rules.

The spacing tokens are observed recurring distances, not a claim that all legacy tools follow a strict scale. Coarse-pointer rules enlarge some targets to 44px, but later component overrides still set smaller heights; do not infer universal 44px compliance.

### Connection page layout

The device handoff page uses a centered maximum width of 1220px, 48px horizontal padding and a two-column grid of 1.12fr / 1fr with a 76px gap. The introduction has 48px top padding; the ivory connection panel uses 36px padding. At 800px and below the sections stack with a 32px gap, page padding becomes 24px, the introduction top padding becomes 20px, and the connection panel padding becomes 24px. This is the connection page's own document flow, not the GM workspace's pane-switching layout.

## Elevation & Depth

The core workbench uses tonal separation, thin rules and the navy/ivory contrast for hierarchy. Standard buttons have no shadow. Dialogs use a soft shadow (`0 20px 60px #0005`), an opaque navy surface and a one-pixel control border above a dark translucent backdrop (`#08182bcc`); the backdrop does not blur.

**The Quiet Surface Rule.** Keep the reading page and standard buttons flat; reserve the documented modal shadow for floating dialogs.

Narrative arrival is a short ease-out transition (160ms) from opacity .75 and translateY(3px) to its resting state. Standard control color changes take 150ms; button press scales to .97. The existing global reduced-motion media query removes animations and transitions. These motion values are in the sidecar because they are outside frontmatter's token schema.

The connection page is flat: its ivory panel has 12px corners without a shadow, blur or entry animation. Its stylesheet does not inherit the workbench's press scale or transition declarations; those behaviors are not implied for its controls.

Not canonized: the inherited thought tooltip still contains violet glow, a small uppercase heading and its own shadow; some advanced tools retain inline color accents and font-based icons. These are legacy drift outside the extracted core system, not reusable design prescriptions. This documentation pass does not repair code.

## Shapes

Control corners use the frontmatter control radius; segmented controls and directory tabs use the smaller segment radius. Dialogs use the dialog radius. The narrative page is flat and borderless; character rows have square corners and a bottom divider instead of enclosing cards. Circular connection indicators and rounded stat bars remain specific functional shapes. Thin rules and compact rectangular controls establish the technical character.

## Components

### Buttons

The primary button is solid Signal Red with white text; hover darkens the fill. The ghost variant is transparent with a navy-gray border on the shell; hover lightens its navy background. On paper, contextual ghost controls instead use dark text and pale borders. Standard buttons have 8px 16px padding and minimum height 38px; small uses 6px 12px / 34px, large 10px 24px / 44px. Disabled buttons use opacity .42, suppress press transforms and show a not-allowed cursor. Retained danger, success and info variants use tinted backgrounds and semantic text; the sidecar samples their dark-shell forms.

All focusable controls inherit a red 2px outline with 4px offset. The sidecar preserves hover, press, focus and disabled behavior in the extracted button examples.

### Inputs / Fields

Dark inputs use Deep Navy, Shell Text, the control border and 11px 12px padding, with 14px UI type. Focus changes the border to red and adds `0 0 0 2px #e5061120`. Light narrative fields use white with navy text and a pale border. Placeholder tones differ by surface. The launch selector is at least 48px high; the action field at least 46px. These are contextual sizes, not the base input height.

### Navigation

Task buttons use 14px sans-serif, muted shell text, and a brighter hover surface. Active text is white and a red 3px underline marks the current group. On mobile the buttons divide the full row and use 13px. The separate scene/directory/party selector uses light surfaces and a filled active state.

The workbench header adds a separate outlined “Connect Devices” link to /connect, opening in a new window with an accessible label naming that behavior. It follows the ghost-control colors and 6px corners, with 13px type, 6px 10px padding and minimum height 38px. At 800px it reduces to a 34px minimum with 6px 8px padding and hides its visual text while retaining the accessible name. The launcher also exposes a plain 14px “Mobile Connection and Sync” link. These are device access points, not a fourth task group.

### Directory Tabs and Metadata

Directory tabs use 13px sans-serif, weight 600, 6px 14px padding and the segment radius. Active tabs use a raised navy fill with a control border. General metadata is the label typography role; compact role badges remain distinct from clickable tabs.

### Character Rows

Character containers are transparent rows with 14px 4px padding, a bottom navy divider and no enclosing border or corner. Names are white, role badges subdued blue, and numeric bars are 3px high. The sidecar represents the row with a real role badge, not a generic elevated card.

### Narrative Branches

Available branches are white, full-width rectangular options on the light action area, with a pale border, 12px 14px padding and minimum height 46px. Hover/focus uses a pale blue fill and stronger border without lifting or adding a shadow. Mono numbering uses dark red; highlighted fate choices use pale red and a darker red border. The narrative column remains the place where the choice is read and acted on.

### Dialogs

Dialogs use the depth and shape rules above. The edit-exit dialog is 460px wide before viewport constraints, padded 30px on desktop and 24px on mobile, with a serif title and readable 14px supporting copy. Its two action buttons align at the end with a 12px gap. Advanced modal layouts retain their content-specific dimensions and scroll constraints.

### Device Connection

The connection panel uses an actual locally generated QR image for the selected GM workbench or NPC communication destination. The white frame has 6px corners and minimum height 208px; the image declares 208px square dimensions with max-width 100%. Its accessible description and adjacent caption explain the destination. The QR endpoint encodes the current connection URL; previews must not substitute invented QR artwork or imply that a static design specimen is a working invitation.

Connection status combines a 7px green dot with readable text. Native 16px radio controls use the existing red accent and select the destination. The copy-address group combines a read-only white input and pale blue copy button within a 6px outlined enclosure; focus within adds a red 2px outline with 2px offset. Global connection-page focus uses the existing red 2px outline with 4px offset. The primary connection action is full width with 13px 20px padding, 15px/600 text and 6px corners; its red hover follows the incumbent accent-hover value. Its busy state uses opacity .55 and a wait cursor, a local difference from the GM button disabled treatment.

The rendered structure distinguishes loading, connection unavailable from this device, disabled, enabled, unavailable address and error states. A native disclosure keeps troubleshooting beneath the action area. Errors use dark red text with role alert; connection counts use role status. These functional states give the QR meaning and should remain visible through text, without creating new decorative status styles.

## Do's and Don'ts

### Do:

- Do preserve the navy frame, ivory page and red action relationship.
- Do use the actual font stacks and responsive sizes recorded here; narrative body and heading fallbacks differ in the shipped CSS.
- Do maintain visible keyboard focus, text labels and reduced-motion support.
- Do keep the narrative, available branches and action composer in one scrollable reading column.
- Do switch the GM workspace to scene, directory and party panes at 800px.

### Don't:

- Don't apply the desktop three-column widths to the mobile GM workbench.
- Don't treat the separate phone.html NPC interface as a redesigned GM screen.
- Don't extend legacy violet tooltip glow, tiny tooltip headings or glyph icons into new system components.
- Don't add decorative pulses or scanlines to the workbench; those effects are disabled in the current override.
