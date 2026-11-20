// The user's text, made safe for a public GitHub issue: nothing in it may
// ping anyone, link or cross-reference another issue (in this repo or any
// other), or render as Markdown or HTML.

// The description's lines are wrapped to this: a code block doesn't wrap.
export const WRAP_WIDTH = 100;

// The description as literal text, in a code block: GitHub makes no links,
// mentions or cross-references there (#123, owner/repo#1, @user, an issue's
// URL), and renders no Markdown or HTML. The fence is longer than any run of
// backticks in the text, so nothing in it can close the block early.
export function plainText(text) {
  const longestRun = Math.max(0, ...(text.match(/`+/g) || []).map((run) => run.length));
  const fence = "`".repeat(Math.max(3, longestRun + 1));
  return `${fence}text\n${wrap(text.replace(/\r\n?/g, "\n"), WRAP_WIDTH)}\n${fence}`;
}

// Breaks lines longer than width at their last space that fits, or mid-word
// when there's none (a long link), so the block never scrolls sideways.
export function wrap(text, width) {
  return text
    .split("\n")
    .map((line) => {
      const out = [];
      let rest = line;
      while (rest.length > width) {
        const space = rest.lastIndexOf(" ", width);
        const cut = space > 0 ? space : width;
        out.push(rest.slice(0, cut).trimEnd());
        rest = rest.slice(space > 0 ? cut + 1 : cut);
      }
      out.push(rest);
      return out.join("\n");
    })
    .join("\n");
}

// The title can't be a code block: a zero-width space after "@" and "#",
// and inside "GH-", so it can't ping anyone or reference an issue.
export function noReferences(text) {
  return text.replace(/[@#]/g, "$&\u200b").replace(/\b(gh)-/gi, "$1\u200b-");
}
