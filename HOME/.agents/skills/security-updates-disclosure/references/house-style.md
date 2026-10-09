# Security announcement house style

Recovered from the 16 maintainer-written Discussion Markdown bodies listed below,
read through `gh api graphql` in October 2026. These are historical examples, not
current release/version or CVE metadata. Fetch at least two security announcements
for the target project on every invocation, preferably recent ones. This reference
does not replace that research.

## Shared conventions

Titles use `<Project> security updates: <version>, <version>, and <version>`.
Recommended releases link to the target project's release tags. The body leads
with exposure and the need to update, without the project-description paragraph
used in ordinary release notes. Explain the vulnerable path and fix in connected
prose; commands, reporter thanks, and the project's short questions invitation
come afterward. Reference-style links are common. CVE labels point to GHSA pages;
GHSA labels lead when a CVE has not yet been assigned.

Distinguish first fixes from later recommended releases. Fedify #1013 and Hollo
#594 explicitly preserve the first-fixed versions while putting newer releases
in the title and installation instructions. Do not change affected ranges to
include the already fixed releases in between.

Some older announcements are much shorter, omit commands, or contain malformed
links. Follow their wording and structure while meeting the current workflow's
requirements for publication checks, reporter credit, upgrade instructions, and
factual accuracy. Severity is not boilerplate: use it
only when supported. Never copy a historical technical limitation as current fact.

## LogTape

#177 is a single-vulnerability prose announcement, opening "If you use
`@logtape/syslog`, update to a patched release now." It names the affected option
and logging path, then explains the vulnerable formatting, fix, release links,
package-specific update commands, and redeployment. Reporter credit is generic
because no public identity is named there. Closing: "If anything is unclear,
ask below."

#226 opens with two advisories, affected packages, and release links. It uses
third-level vulnerability headings and `### Updating`. It distinguishes safe
interpolation from untrusted literal messages, default sanitization from opt-in
newline escaping, and TCP from UDP. It credits independent reporters per issue.
Do not mechanically mandate heading depth for future LogTape announcements;
compare the most recent live examples.

Sources:

- https://github.com/dahlia/logtape/discussions/177
- https://github.com/dahlia/logtape/discussions/226

## Fedify

Recent posts open "If you use an affected Fedify release, update now" or "If you
use Fedify, update to a patched release now", followed by identifiers and impact.
One or two issues can be explained in unheaded paragraphs (#796, #954, #1013).
Larger posts use second-level headings with the vulnerability name, primary
identifier, severity, and CVSS when documented (#1075, #1221), then `Updating`.

Update commands are aligned across npm, Yarn, pnpm, Bun, and Deno for
`@fedify/fedify`; direct dependencies on other affected Fedify packages are named
when relevant. Check resolved versions and redeploy. Older unsupported majors
need a dependency-range change, not just a normal package-manager update.
Reporter thanks map people to reports. Closing: "If anything is unclear, ask
below" (or the closely related "Ask below if anything is unclear").

#91 and #97 show the earlier short form and follow-up hardening after a prior
fix. They are useful historical context, not a reason to omit today's required
upgrade instructions or reporter acknowledgment. #1221 demonstrates GHSA-first
naming before CVE assignment; recheck metadata for any new draft.

Sources:

- https://github.com/fedify-dev/fedify/discussions/1221
- https://github.com/fedify-dev/fedify/discussions/1075
- https://github.com/fedify-dev/fedify/discussions/1013
- https://github.com/fedify-dev/fedify/discussions/954
- https://github.com/fedify-dev/fedify/discussions/796
- https://github.com/fedify-dev/fedify/discussions/97
- https://github.com/fedify-dev/fedify/discussions/91

## BotKit

#44 and #51 open "If you use BotKit, update to a patched release now" and identify
Fedify as the vulnerable dependency. They describe issues in unheaded prose,
map BotKit versions to bundled Fedify versions, and provide separate command
blocks for each maintained BotKit line. #44's circuit-breaker flaw affects only
one of its two BotKit lines; do not imply a uniform affected range.

Upgrade `@fedify/botkit`, check resolved upstream versions when needed, and
redeploy. Link upstream GHSAs and "Fedify's own announcement". Thank the upstream
reporters. Closing: "If anything is unclear, feel free to ask on [GitHub
Discussions] or [Matrix]." Verify these destinations in current announcements.
Historical version-qualified `update` commands must be checked against the
actual package manager before reuse.

Sources:

- https://github.com/fedify-dev/botkit/discussions/51
- https://github.com/fedify-dev/botkit/discussions/44

## Hollo

These posts lead "If you run Hollo, update to a patched release now", identify
the vulnerable Fedify component, and explain its concrete reachability in Hollo.
They use unheaded paragraphs, link the upstream advisory and announcement,
then list affected and patched Hollo versions per release line. Link Hollo's
security policy when explaining unsupported lines.

#594 explicitly says the upstream circuit-breaker advisory does not affect its
supported lines, whose Fedify versions predate that feature. Both upstream
advisories still need publication checks if both appear in the announcement.
#625 maps fixed Hollo releases to fixed Fedify releases and distinguishes the
automatically invalidated built-in key cache from library-level custom caches.

Use per-line `docker pull ghcr.io/fedify-dev/hollo:<version>` blocks, followed by
container restart instructions and instructions for source deployments using the corresponding release tag.
Credit reporters for responsible disclosure *to the Fedify project* when that
is where they reported. Closing: "If anything is unclear, ask below."

Sources:

- https://github.com/fedify-dev/hollo/discussions/625
- https://github.com/fedify-dev/hollo/discussions/594
- https://github.com/fedify-dev/hollo/discussions/555
- https://github.com/fedify-dev/hollo/discussions/516
- https://github.com/fedify-dev/hollo/discussions/497
