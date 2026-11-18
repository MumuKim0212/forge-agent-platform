// invisible and
// direction-changing characters are made VISIBLE, never interpreted, in any string the
// agent chose (a path, a finding value, the agent's summary) and in the requester's text
// shown to anyone else (spec: any string shown to someone other than its author). Otherwise `report\u{202E}txt.py`
// would display as a different file name, and so would `app/main`, a zero-width space and `.py`.
//
// The set is the spec's: the Unicode bidirectional controls U+202A-U+202E and U+2066-U+2069, the
// marks U+200E / U+200F / U+061C, DEL (U+007F), the C0 (U+0000-U+001F) and C1 (U+0080-U+009F)
// controls EXCEPT newline and tab, which text legitimately contains, and (widened 2026-10-17) the
// zero-width and other default-ignorable characters: U+200B-U+200D, U+2060-U+2064, U+FEFF,
// U+00AD, U+034F, U+180E and the tag block U+E0000-U+E007F. The spec says the list is the
// principle made concrete, not a closed set ("a character that renders as nothing is made
// visible"), so two more groups that render as nothing and never occur in text people write here
// are in it (each pinned by `visibleControls.test.ts`): U+2065 and U+206A-U+206F, the rest
// of that invisible-formatting block (deprecated format controls), and the four Hangul fillers
// U+115F, U+1160, U+3164 and U+FFA0, which render as blank Korean letters and are the classic way
// to make a name look empty. Variation selectors (U+FE00-U+FE0F) are NOT in it: U+FE0F is part of
// ordinary emoji.
//
// One exception inside the set, and only one: a ZERO WIDTH JOINER (U+200D) between two emoji is
// the glue of a single emoji (a family, a skin tone, a heart on fire) and is left alone. A ZWJ
// anywhere else -- between letters, next to a path separator, at either end -- is marked. The
// astral tag characters are matched whole (the `u` flag walks code points, not UTF-16 units), and
// the marker for one carries its real code point (U+E0041, not half a surrogate pair).
//
// A CR+LF pair is a line break, not two controls: it is normalised to LF first, so Windows
// text is not littered with markers; a CR on its own (nothing follows it as LF) is marked.
// Each becomes `⟨U+XXXX⟩` (four or more upper-case hex digits). The marker itself holds
// no invisible character, so the function is idempotent.
//
// This file holds none of these characters as itself, only as `\u` escapes: a raw invisible
// character in a source file is the same hazard aimed at a reviewer. Build one from its code point.
// (An editing tool that "helpfully" turns `\u{202E}` text into the real character has done exactly
// that here before; `git diff` the escapes after any edit, and see backend/tests/test_no_raw_bidi_controls.py.)

import type { ReactNode } from "react";

const CRLF = /\x0D\x0A/g;
const ZERO_WIDTH_JOINER = "\u{200D}";
const INVISIBLE =
  /[\u0000-\u0008\u000B-\u001F\u007F-\u009F\u{AD}\u{34F}\u{61C}\u{115F}\u{1160}\u{180E}\u{200B}-\u{200F}\u{202A}-\u{202E}\u{2060}-\u{206F}\u{3164}\u{FEFF}\u{FFA0}\u{E0000}-\u{E007F}]/gu;
// An emoji (with its optional presentation selector or skin-tone modifier) ending right before a
// ZWJ, and an emoji starting right after it: the two halves of a ZWJ sequence.
const EMOJI_ENDING = /\p{Extended_Pictographic}[\u{FE0F}\u{1F3FB}-\u{1F3FF}]?$/u;
const EMOJI_STARTING = /^\p{Extended_Pictographic}/u;

/** True for a ZWJ at `offset` of `whole` that sits between two emoji. Looks at a window of at most
 * four UTF-16 units before it (an astral emoji plus an astral modifier) and two after. */
function joinsEmoji(whole: string, offset: number): boolean {
  return (
    EMOJI_ENDING.test(whole.slice(Math.max(0, offset - 4), offset)) &&
    EMOJI_STARTING.test(whole.slice(offset + 1, offset + 3))
  );
}

function marker(ch: string): string {
  return `⟨U+${(ch.codePointAt(0) as number).toString(16).toUpperCase().padStart(4, "0")}⟩`;
}

/** `text` with every invisible / direction-changing character replaced by a visible `⟨U+XXXX⟩`. */
export function visibleControls(text: string): string {
  return text
    .replace(CRLF, "\n")
    .replace(INVISIBLE, (ch, offset: number, whole: string) =>
      ch === ZERO_WIDTH_JOINER && joinsEmoji(whole, offset) ? ch : marker(ch),
    );
}

/** `visibleControls` over a React child: a string is converted, an array is converted element-wise, anything else is left alone. */
export function visibleControlsNode(node: ReactNode): ReactNode {
  if (typeof node === "string") return visibleControls(node);
  if (Array.isArray(node)) return node.map((child) => visibleControlsNode(child));
  return node;
}
