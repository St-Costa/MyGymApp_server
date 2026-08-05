// Enforces the Conventional Commits format documented in CLAUDE.md
// § "Commit message format". Run via lefthook's commit-msg hook.
// https://commitlint.js.org

module.exports = {
  extends: ["@commitlint/config-conventional"],
  rules: {
    "type-enum": [
      2,
      "always",
      ["feat", "fix", "refactor", "docs", "test", "chore", "perf", "style", "build", "ci"],
    ],
    "subject-case": [0],
  },
};
