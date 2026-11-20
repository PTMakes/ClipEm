// npm test. The user's text in a public issue: literal, and linking nothing.

import assert from "node:assert/strict";
import test from "node:test";

import { noReferences, plainText, WRAP_WIDTH, wrap } from "../src/issue_text.js";

// The text inside plainText()'s block, and its fence.
function unfence(block) {
  const match = /^(`{3,})text\n([\s\S]*)\n\1$/.exec(block);
  assert.ok(match, `not one fenced block: ${JSON.stringify(block)}`);
  return { fence: match[1], text: match[2] };
}

// Whether a line closes a block opened with fence (CommonMark: the same
// character, at least as many, nothing else on the line but spaces).
function closes(line, fence) {
  const match = /^ {0,3}(`+)\s*$/.exec(line);
  return Boolean(match) && match[1].length >= fence.length;
}

test("plain text comes back exactly, in a text block", () => {
  const text = "It crashed when I saved a clip.\n\n  - indented\n\t# not a heading\n<b>not bold</b> **nor this**";
  assert.equal(plainText(text), `\`\`\`text\n${text}\n\`\`\``);
});

test("references, mentions and links are only text inside the block", () => {
  const text = "@someone #12 GH-3 other/repo#4 https://github.com/other/repo/issues/5 ![x](https://e.com/x.png)";
  assert.equal(unfence(plainText(text)).text, text);
});

for (const text of [
  "```",
  "```\nbreak out\n```",
  "``````",
  "a ```` b",
  "   ```",
  "`",
  "ends with backticks ``",
  "```text\nnested\n```",
]) {
  test(`backticks can't close the block early: ${JSON.stringify(text)}`, () => {
    const block = plainText(text);
    const { fence, text: inside } = unfence(block);
    assert.equal(inside, text);
    assert.ok(!inside.split("\n").some((line) => closes(line, fence)));
  });
}

test("tildes don't need a longer fence: they can't close a backtick block", () => {
  assert.equal(unfence(plainText("~~~~~~\nstill inside")).fence, "```");
});

test("Windows and old Mac line endings become \\n", () => {
  assert.equal(unfence(plainText("one\r\ntwo\rthree")).text, "one\ntwo\nthree");
});

test("long lines wrap at a space, so the block doesn't scroll sideways", () => {
  const words = Array.from({ length: 60 }, (_, i) => `word${i}`).join(" ");
  const lines = unfence(plainText(words)).text.split("\n");
  assert.ok(lines.length > 1);
  assert.ok(lines.every((line) => line.length <= WRAP_WIDTH));
  assert.equal(lines.join(" "), words); // only spaces were turned into breaks
});

test("a word longer than a line is cut", () => {
  const url = `https://example.com/${"a".repeat(250)}`;
  const lines = wrap(url, WRAP_WIDTH).split("\n");
  assert.deepEqual(lines.map((line) => line.length), [100, 100, 70]);
  assert.equal(lines.join(""), url);
});

test("short lines and blank lines are kept as they are", () => {
  assert.equal(wrap("a\n\n  b  \n", 10), "a\n\n  b  \n");
  assert.equal(wrap("x".repeat(10), 10), "x".repeat(10));
});

test("a line breaks at the last space that fits", () => {
  assert.equal(wrap("aaa bbb ccc", 7), "aaa bbb\nccc");
  assert.equal(wrap("aaa    bbb", 5), "aaa\n bbb"); // the space it breaks at goes; the rest stay
});

test("the title can't ping or reference anything", () => {
  assert.equal(noReferences("@a #1 GH-2 gh-3 o/r#4"), "@​a #​1 GH​-2 gh​-3 o/r#​4");
  assert.equal(noReferences("Crashed on save"), "Crashed on save");
  assert.equal(noReferences("high-res"), "high-res"); // only a whole "GH-"
});
