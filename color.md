# nfsmon — available colors & attributes

Values for the `[colors]` section in `nfsmon.conf` and in theme files
(`~/.config/nfsmon/colors/<name>.conf`). Per-entry format:

```
<role> = <fg_color>[,<attr>[,<attr>...]]
```

`<fg_color>` is either a **name** (see tables below) or a
**256-color number** as a digit (0-255, e.g. `208` for orange).

## 8-color base (every terminal supports this)

| Name      | Note                                                     |
| --------- | -------------------------------------------------------- |
| `default` | Terminal default (no pair, uses the terminal's fg)       |
| `black`   | Code 0                                                   |
| `red`     | Code 1                                                   |
| `green`   | Code 2                                                   |
| `yellow`  | Code 3                                                   |
| `blue`    | Code 4                                                   |
| `magenta` | Code 5                                                   |
| `cyan`    | Code 6                                                   |
| `white`   | Code 7                                                   |

Bright variants of the base 8 are available via `,bold` — on most
terminals `red,bold` renders as bright-red.

## 256-color aliases

Require a 256-color-capable terminal (`$TERM` typically contains
`-256color`). On older terminals these are silently ignored and the
role falls back to its built-in default color — no crash, no warning.

| Alias          | Code | Description                   |
| -------------- | ---- | ----------------------------- |
| `gray`         | 244  | mid gray                      |
| `darkgray`     | 240  | dark gray                     |
| `lightgray`    | 250  | light gray                    |
| `silver`       | 145  | muted silver-white            |
| `darkred`      |  88  | dark red, bordeaux            |
| `darkgreen`    |  22  | very dark green               |
| `lightgreen`   | 120  | light pastel green            |
| `lime`         |  46  | vivid green                   |
| `mint`         | 121  | mint green                    |
| `darkyellow`   | 136  | mustard yellow                |
| `gold`         | 220  | golden yellow                 |
| `orange`       | 208  | classic orange                |
| `darkorange`   | 166  | red-tinged orange             |
| `peach`        | 216  | peach orange                  |
| `darkblue`     |  17  | deep blue                     |
| `lightblue`    | 117  | light blue                    |
| `navy`         |  18  | navy / night blue             |
| `teal`         |  30  | petrol / teal                 |
| `royalblue`    |  27  | rich royal blue               |
| `darkcyan`     |  24  | dark cyan                     |
| `lightcyan`    | 159  | very light cyan               |
| `purple`       |  91  | dark purple                   |
| `lightpurple`  | 141  | lavender                      |
| `pink`         | 205  | pink                          |
| `hotpink`      | 199  | vivid hot pink                |
| `lightpink`    | 217  | light pink                    |
| `brown`        |  94  | earthy brown                  |
| `tan`          | 180  | sandy / tan                   |

Other 256 codes can be specified directly as a number:
```ini
title = 33,bold        # cobalt blue
text  = 250            # light gray
```

A full 256-color table (every code with a preview) can be printed in
the terminal with:
```
for i in {0..255}; do printf "\e[38;5;${i}m%3d \e[0m" "$i"; ((i%16==15)) && echo; done
```

## Attributes

Comma-separated after the color. Order doesn't matter; multi-attribute
combinations are allowed.

| Name        | Effect                                                                |
| ----------- | --------------------------------------------------------------------- |
| `bold`      | bold / bright variant of the color                                    |
| `dim`       | dimmed / darkened                                                     |
| `blink`     | blinking (terminal-dependent — some emulators ignore it)              |
| `reverse`   | foreground/background swapped                                         |
| `underline` | underlined                                                            |
| `standout`  | "stands out" — usually like reverse, terminal-specific                |
| `normal`    | explicitly no attribute (rarely needed)                               |

## Roles (what's colored)

| Role              | Where it appears                                      | Default            |
| ----------------- | ----------------------------------------------------- | ------------------ |
| `title`           | Title row + popup borders + popup titles              | `cyan,bold`        |
| `key_bar`         | Tab bar in the o-popup, save button (focused), active tab | `yellow,bold`  |
| `text`            | Popup body text, inactive tabs (with dim)             | `green`            |
| `footer`          | Bottom status row + help headers                      | `cyan`             |
| `alert`           | Row whose throughput exceeds the alert threshold      | `red,bold,blink`   |
| `idle`            | Rows with no throughput, ghost row                    | `white,dim`        |
| `activity_low`    | Rows with < 1 MB/s                                    | `green`            |
| `activity_medium` | Rows with 1-10 MB/s                                   | `yellow,bold`      |
| `activity_high`   | Rows with ≥ 10 MB/s                                   | `red,bold`         |

## Examples

```ini
[colors]
title           = blue,bold
key_bar         = magenta,bold,reverse
text            = white
footer          = cyan
alert           = red,bold,blink
idle            = default,dim
activity_low    = green
activity_medium = yellow,bold
activity_high   = red,bold,underline
```

## Bright-sort mode

When `Bright sorted column` is active in the o-popup, the actively
sorted column is additionally re-rendered per row with
`(attr & ~A_DIM) | A_BOLD` — so: `dim` is stripped, `bold` is added.
That makes every role visibly brighter. Themes need no special
treatment for this.

## Unknown values

Unknown color or attribute names are silently ignored (`default` and
no attribute respectively). So just fix the typo — the tool doesn't
crash.
