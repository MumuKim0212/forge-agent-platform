/**
 * Invisible and direction-changing characters are made visible
 *. Decision logic over strings, so
 * no mounting (ADR 0021). The `Code` test calls the function component directly: it has no
 * hooks, and what it returns is the element tree whose text a browser would show.
 *
 * The set also covers zero-width and other default-ignorable characters and
 * the tag block. Every character here is built from its code point or written as an escape: this
 * file must hold none of them as itself.
 */
import type { ReactElement } from "react";
import { describe, expect, it } from "vitest";

import { Code } from "./Code";
import { visibleControls, visibleControlsNode } from "./visibleControls";

/** The code point inside the marker of `a<marker>b`, read off a result: null when the text is not
 * exactly that shape (so a result that left the character alone, or marked it twice, is null). */
function markedCodePoint(marked: string): number | null {
  const open = "a⟨U+";
  const close = "⟩b";
  if (!marked.startsWith(open) || !marked.endsWith(close)) return null;
  const hex = marked.slice(open.length, marked.length - close.length);
  return /^[0-9A-F]{4,}$/.test(hex) ? parseInt(hex, 16) : null;
}

describe("visibleControls", () => {
  it.each([
    ["\u{202A}", "⟨U+202A⟩"],
    ["\u{202B}", "⟨U+202B⟩"],
    ["\u{202C}", "⟨U+202C⟩"],
    ["\u{202D}", "⟨U+202D⟩"],
    ["\u{202E}", "⟨U+202E⟩"],
    ["\u{2066}", "⟨U+2066⟩"],
    ["\u{2067}", "⟨U+2067⟩"],
    ["\u{2068}", "⟨U+2068⟩"],
    ["\u{2069}", "⟨U+2069⟩"],
    ["\u{200E}", "⟨U+200E⟩"],
    ["\u{200F}", "⟨U+200F⟩"],
    ["\u{61C}", "⟨U+061C⟩"],
  ])("marks the direction control %j", (ch, marker) => {
    expect(visibleControls(`a${ch}b`)).toBe(`a${marker}b`);
  });

  it("marks C0 controls (NUL, bell, escape, carriage return, unit separator) and C1 controls", () => {
    expect(visibleControls("\u0000")).toBe("⟨U+0000⟩");
    expect(visibleControls("\u0007")).toBe("⟨U+0007⟩");
    expect(visibleControls("\u001B[31m")).toBe("⟨U+001B⟩[31m");
    expect(visibleControls("a\rb")).toBe("a⟨U+000D⟩b");
    expect(visibleControls("\u001F")).toBe("⟨U+001F⟩");
    expect(visibleControls("\u0080")).toBe("⟨U+0080⟩");
    expect(visibleControls("\u0085")).toBe("⟨U+0085⟩");
    expect(visibleControls("\u009F")).toBe("⟨U+009F⟩");
  });

  it("marks DEL (U+007F)", () => {
    expect(visibleControls("a\x7Fb")).toBe("a⟨U+007F⟩b");
  });

  it("normalises CRLF to LF but still marks a lone CR", () => {
    expect(visibleControls("a\x0D\x0Ab\x0D\x0Ac")).toBe("a\nb\nc");
    expect(visibleControls("a\x0Db")).toBe("a⟨U+000D⟩b");
    expect(visibleControls("a\x0D\x0D\x0Ab")).toBe("a⟨U+000D⟩\nb");
    expect(visibleControls("a\x0A\x0Db")).toBe("a\n⟨U+000D⟩b");
    expect(visibleControls("a\x0D")).toBe("a⟨U+000D⟩");
  });

  it("keeps newline and tab", () => {
    expect(visibleControls("a\nb\tc")).toBe("a\nb\tc");
  });

  it("shows the spec example: report then an override then txt.py", () => {
    expect(visibleControls("report\u{202E}txt.py")).toBe("report⟨U+202E⟩txt.py");
  });

  it("is idempotent: an already-marked string is not marked again", () => {
    const once = visibleControls("x\u{202E}y\u0007z");
    expect(once).toBe("x⟨U+202E⟩y⟨U+0007⟩z");
    expect(visibleControls(once)).toBe(once);
  });
});

/**
 * The set, group by group, written out here as literals from the spec so a group cannot vanish
 * from the production regex without exactly its test going red. The first five groups were
 * the original set, the next seven widen it, the last two go beyond its list
 * by its own principle.
 */
const GROUPS: ReadonlyArray<{ name: string; ranges: ReadonlyArray<readonly [number, number]> }> = [
  { name: "C0 controls except tab and line feed", ranges: [[0x00, 0x08], [0x0b, 0x1f]] },
  { name: "DEL and the C1 controls", ranges: [[0x7f, 0x9f]] },
  { name: "bidirectional embeddings and overrides U+202A-U+202E", ranges: [[0x202a, 0x202e]] },
  { name: "bidirectional isolates U+2066-U+2069", ranges: [[0x2066, 0x2069]] },
  { name: "directional marks U+200E, U+200F and U+061C", ranges: [[0x200e, 0x200f], [0x61c, 0x61c]] },
  { name: "zero-width space, non-joiner and joiner U+200B-U+200D", ranges: [[0x200b, 0x200d]] },
  { name: "word joiner and invisible operators U+2060-U+2064", ranges: [[0x2060, 0x2064]] },
  { name: "byte order mark / zero-width no-break space U+FEFF", ranges: [[0xfeff, 0xfeff]] },
  { name: "soft hyphen U+00AD", ranges: [[0xad, 0xad]] },
  { name: "combining grapheme joiner U+034F", ranges: [[0x34f, 0x34f]] },
  { name: "Mongolian vowel separator U+180E", ranges: [[0x180e, 0x180e]] },
  { name: "the tag block U+E0000-U+E007F", ranges: [[0xe0000, 0xe007f]] },
  { name: "deprecated format controls U+2065 and U+206A-U+206F", ranges: [[0x2065, 0x2065], [0x206a, 0x206f]] },
  { name: "Hangul fillers U+115F, U+1160, U+3164 and U+FFA0", ranges: [[0x115f, 0x1160], [0x3164, 0x3164], [0xffa0, 0xffa0]] },
];

describe("visibleControls marks every group of the set, each code point of it", () => {
  for (const group of GROUPS) {
    it(group.name, () => {
      for (const [first, last] of group.ranges) {
        for (let cp = first; cp <= last; cp += 1) {
          const marked = visibleControls(`a${String.fromCodePoint(cp)}b`);
          expect(markedCodePoint(marked), `U+${cp.toString(16).toUpperCase()}`).toBe(cp);
        }
      }
    });
  }

  it("spells the marker with the real code point, an astral one included, never half a surrogate pair", () => {
    expect(visibleControls("x\u{E0041}y")).toBe("x⟨U+E0041⟩y");
    expect(visibleControls("\u{E0000}")).toBe("⟨U+E0000⟩");
    expect(visibleControls("\u{E007F}")).toBe("⟨U+E007F⟩");
    expect(visibleControls("a\u{200B}b")).toBe("a⟨U+200B⟩b");
    expect(visibleControls("\u{FEFF}a")).toBe("⟨U+FEFF⟩a");
    expect(visibleControls("co\u{AD}op")).toBe("co⟨U+00AD⟩op");
  });

  it("shows the finding that widened the spec: a zero-width space in a file name", () => {
    expect(visibleControls("app/main\u{200B}.py")).toBe("app/main⟨U+200B⟩.py");
  });

  it("marks each tag character beside the emoji it was hidden next to, leaving the emoji", () => {
    // A black flag followed by tag characters (the shape a subdivision flag, or a hidden payload, takes).
    const flag = "\u{1F3F4}\u{E0067}\u{E0062}\u{E007F}";
    expect(visibleControls(flag)).toBe("\u{1F3F4}⟨U+E0067⟩⟨U+E0062⟩⟨U+E007F⟩");
  });

  it("covers exactly this set over all of Unicode: nothing outside it changes, nothing inside it is left", () => {
    const expected: number[] = [];
    for (const group of GROUPS) {
      for (const [first, last] of group.ranges) {
        for (let cp = first; cp <= last; cp += 1) expected.push(cp);
      }
    }
    expected.sort((a, b) => a - b);

    const changed: number[] = [];
    for (let cp = 0; cp <= 0x10ffff; cp += 1) {
      const ch = String.fromCodePoint(cp);
      if (visibleControls(ch) !== ch) changed.push(cp);
    }

    expect(changed).toEqual(expected);
  });

  it("is idempotent over the widened set too", () => {
    const once = visibleControls("a\u{200B}b\u{E0041}c\u{2060}d\u{FEFF}e");
    expect(once).toBe("a⟨U+200B⟩b⟨U+E0041⟩c⟨U+2060⟩d⟨U+FEFF⟩e");
    expect(visibleControls(once)).toBe(once);
  });
});

describe("visibleControls leaves ordinary text alone (no false positives)", () => {
  it("leaves ordinary text, Korean and markup characters unchanged", () => {
    for (const text of ["", "plain text", "한글 텍스트와 공백", "<b>x</b> & http://a.example/?q=1", "app/app.py"]) {
      expect(visibleControls(text)).toBe(text);
    }
  });

  it("leaves a Korean sentence with an emoji, a family and a presentation selector unchanged", () => {
    const family = "\u{1F468}\u{200D}\u{1F469}\u{200D}\u{1F467}";
    const heart = "\u{2764}\u{FE0F}";
    const sentence = `프로젝트 이름은 한글입니다 \u{1F642} 가족 ${family} 사랑 ${heart} 완료!`;
    expect(visibleControls(sentence)).toBe(sentence);
  });

  it("leaves combining marks and decomposed Hangul alone", () => {
    for (const text of [
      "e\u{301}",
      "a\u{308}\u{323}",
      "\u{1112}\u{1161}\u{11AB}\u{1100}\u{1173}\u{11AF}", // conjoining jamo spelling a Hangul word
      "1\u{FE0F}\u{20E3}", // a keycap
    ]) {
      expect(visibleControls(text)).toBe(text);
    }
  });
});

describe("a zero width joiner is left alone only inside an emoji sequence", () => {
  const MAN = "\u{1F468}";
  const WOMAN = "\u{1F469}";
  const ZWJ = "\u{200D}";

  it("leaves a family, a skin-tone profession, a heart on fire and a rainbow flag unchanged", () => {
    for (const sequence of [
      `${MAN}${ZWJ}${WOMAN}${ZWJ}\u{1F467}`,
      `${WOMAN}\u{1F3FD}${ZWJ}\u{1F4BB}`,
      `\u{2764}\u{FE0F}${ZWJ}\u{1F525}`,
      `\u{1F3F3}\u{FE0F}${ZWJ}\u{1F308}`,
      `\u{1F937}${ZWJ}\u{2640}\u{FE0F}`,
    ]) {
      expect(visibleControls(sequence)).toBe(sequence);
    }
  });

  it("marks a lone joiner, and one between letters or beside a path separator", () => {
    expect(visibleControls(ZWJ)).toBe("⟨U+200D⟩");
    expect(visibleControls(`ma${ZWJ}in`)).toBe("ma⟨U+200D⟩in");
    expect(visibleControls(`app/${ZWJ}main.py`)).toBe("app/⟨U+200D⟩main.py");
    expect(visibleControls(`한${ZWJ}글`)).toBe("한⟨U+200D⟩글");
  });

  it("marks a joiner with an emoji on only one side, either side", () => {
    expect(visibleControls(`${MAN}${ZWJ}x`)).toBe(`${MAN}⟨U+200D⟩x`);
    expect(visibleControls(`x${ZWJ}${MAN}`)).toBe(`x⟨U+200D⟩${MAN}`);
    expect(visibleControls(`${MAN}${ZWJ}`)).toBe(`${MAN}⟨U+200D⟩`);
    expect(visibleControls(`${ZWJ}${MAN}`)).toBe(`⟨U+200D⟩${MAN}`);
  });

  it("marks both of two joiners in a row, and the other zero-width characters between emoji", () => {
    expect(visibleControls(`${MAN}${ZWJ}${ZWJ}${WOMAN}`)).toBe(`${MAN}⟨U+200D⟩⟨U+200D⟩${WOMAN}`);
    expect(visibleControls(`${MAN}\u{200B}${WOMAN}`)).toBe(`${MAN}⟨U+200B⟩${WOMAN}`);
    expect(visibleControls(`${MAN}\u{200C}${WOMAN}`)).toBe(`${MAN}⟨U+200C⟩${WOMAN}`);
  });
});

describe("visibleControlsNode", () => {
  it("converts a string and the strings of an array, and leaves other nodes alone", () => {
    expect(visibleControlsNode("a\u{202E}b")).toBe("a⟨U+202E⟩b");
    expect(visibleControlsNode(["a\u{202E}", 5, null])).toEqual(["a⟨U+202E⟩", 5, null]);
    expect(visibleControlsNode(null)).toBeNull();
  });
});

describe("ui/Code applies it", () => {
  const textOf = (el: ReactElement) => (el.props as { children: unknown }).children;

  it("shows a path containing a right-to-left override with the marker", () => {
    const el = Code({ children: "app/report\u{202E}txt.py" });
    expect(textOf(el)).toBe("app/report⟨U+202E⟩txt.py");
  });

  it("shows a path containing a zero-width space with the marker", () => {
    expect(textOf(Code({ children: "app/main\u{200B}.py" }))).toBe("app/main⟨U+200B⟩.py");
  });

  it("marks every string child and leaves ordinary paths as they are", () => {
    expect(textOf(Code({ children: "app/한글.py" }))).toBe("app/한글.py");
    expect(textOf(Code({ children: ["a\u0007", "b"] }))).toEqual(["a⟨U+0007⟩", "b"]);
  });
});
